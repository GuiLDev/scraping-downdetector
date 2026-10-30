"""API de coleta pelo navegador (so existe com `create_app(..., coleta_api=True)`).

A extensao do navegador le o HTML da aba que o USUARIO ja abriu e o envia aqui. O servidor nao
acessa a internet, nao le cookies e nao interage com desafios: se a pagina vier bloqueada, a
coleta PAUSA e so volta a verificar quando o usuario resolver o bloqueio manualmente na aba e
clicar em "Retomar verificacao" da origem (que apenas autoriza a extensao a LER a pagina atual, sem
recarregar). O estado e por origem: `POST /coleta/retomar` recebe o campo `origem` (validado contra a lista).

Fluxo da extensao: `GET /api/coleta/estado` (so consulta) -> `POST /api/coleta/reserva` (reserva a
vaga ANTES de recarregar) -> recarga -> `POST /api/coleta` com o `reserva_id`. No estado
`verificando` ela envia o DOM atual com `modo: "verificacao"`, sem recarga nem reserva.

Com `leitura = "pontual"` na config, so existe `modo: "pontual"`: UMA leitura manual do DOM ja
carregado, pelo caminho NORMAL da reserva (`reserva_id` obrigatorio, nunca o modo `verificacao`).
Sem esse modo, uma config pontual responde 409; com config normal, `pontual` e "modo desconhecido".

Este modulo so trata HTTP; as gravacoes ficam em `bankmon.browser_service`.
O token (`X-BankMonitor-Token`) nunca e logado nem devolvido em respostas.
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime
from typing import Callable

from flask import Flask, jsonify, redirect, request, url_for

from bankmon import browser_service, origins, runner
from bankmon.config import Config
from bankmon.timeutil import to_iso

TOKEN_HEADER = "X-BankMonitor-Token"
MAX_BODY_BYTES = 6 * 1024 * 1024
PONTUAL_MODE = "pontual"  # leitura manual do DOM ja carregado; so com `leitura = "pontual"` na config


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def register(
    app: Flask,
    cfg: Config,
    clock: Callable[[], datetime],
    token: str,
    csrf_token: str,
    error_page: Callable[[int], object],
) -> None:
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

    def authorized() -> bool:
        return _same(request.headers.get(TOKEN_HEADER, ""), token)

    def refuse(code: int, message: str):
        return jsonify(erro=message), code

    def json_body() -> dict | None:
        raw = request.get_data()  # 413 se passar de MAX_CONTENT_LENGTH
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):  # JSON invalido ou aninhado demais
            return None
        return body if isinstance(body, dict) else None

    @app.route("/api/coleta/reserva", methods=["POST"], provide_automatic_options=False)
    def api_reserve():
        if not authorized():
            return refuse(401, "token ausente ou inválido")
        body = json_body()
        alvo = body.get("alvo") if body else None
        if not isinstance(alvo, str) or not alvo:
            return refuse(400, "corpo esperado: JSON {alvo}")
        if not origins.is_authorized(alvo, cfg.browser_origins):
            return refuse(403, "origem não autorizada")
        modo = body.get("modo")
        if cfg.browser_pontual:
            if modo != PONTUAL_MODE:
                return refuse(409, 'config de leitura pontual: a reserva exige modo "pontual"')
        elif modo is not None:
            return refuse(400, "modo desconhecido")
        try:
            reservation = browser_service.reserve(cfg, alvo, clock())
        except browser_service.Paused:
            return refuse(409, "pausado")
        except browser_service.ReservationActive:
            return refuse(409, "reserva_ativa")
        except browser_service.TooFrequent as exc:
            resp = refuse(429, str(exc))
            resp[0].headers["Retry-After"] = str(exc.retry_after)
            return resp
        except runner.LockBusy:
            return refuse(503, "outra coleta em andamento; tente novamente")
        return jsonify(reserva_id=reservation.reserva_id, expira_em=to_iso(reservation.expira_em))

    @app.route("/api/coleta", methods=["POST"], provide_automatic_options=False)
    def api_collect():
        if not authorized():
            return refuse(401, "token ausente ou inválido")
        body = json_body()
        if body is None or not isinstance(body.get("html"), str):
            return refuse(400, "corpo esperado: JSON {url, html, reserva_id | modo}")
        reserva_id, modo = body.get("reserva_id"), body.get("modo")
        if reserva_id is not None and not isinstance(reserva_id, str):
            return refuse(400, "reserva_id deve ser texto")
        if modo not in (None, browser_service.VERIFICATION_MODE) and not (
            modo == PONTUAL_MODE and cfg.browser_pontual
        ):
            return refuse(400, "modo desconhecido")
        if cfg.browser_pontual and modo != PONTUAL_MODE:
            return refuse(409, 'config de leitura pontual: só aceita modo "pontual"')
        url = body.get("url")
        if not origins.is_authorized(url, cfg.browser_origins):
            return refuse(403, "origem não autorizada")
        try:
            out = browser_service.ingest(
                cfg, url, body["html"], clock(), reserva_id=reserva_id,
                verification=modo == browser_service.VERIFICATION_MODE,
                label="leitura pontual" if modo == PONTUAL_MODE else "",
            )
        except browser_service.Paused:
            return refuse(409, "coleta pausada: veja o painel")
        except browser_service.VerificationRefused:
            return refuse(409, "verificação indisponível neste estado")
        except browser_service.TooFrequent as exc:
            return refuse(429, str(exc))
        except runner.LockBusy:
            return refuse(503, "outra coleta em andamento; tente novamente")
        return jsonify(
            estado=out.estado, run_id=out.run_id, page_state=out.page_state, servicos=out.servicos
        )

    @app.route("/api/coleta/estado", methods=["GET"], provide_automatic_options=False)
    def api_state():
        if not authorized():
            return refuse(401, "token ausente ou inválido")
        alvo = request.args.get("alvo")  # obrigatorio: origem fora da lista (ou revogada) -> 403
        if not alvo:
            return refuse(400, "parâmetro alvo obrigatório")
        if not origins.is_authorized(alvo, cfg.browser_origins):
            return refuse(403, "origem não autorizada")
        state = browser_service.read_state(cfg, alvo)
        available = browser_service.check_availability(cfg, alvo, clock())
        return jsonify(
            estado=state.estado,
            motivo=state.motivo,
            desde=to_iso(state.desde) if state.desde else None,
            pode_reservar=available.pode_reservar,
            proxima_leitura_em=to_iso(available.proxima_leitura_em) if available.proxima_leitura_em else None,
        )

    @app.route("/coleta/retomar", methods=["POST"], provide_automatic_options=False)
    def resume_collection():
        if not _same(request.form.get("csrf", ""), csrf_token):
            return error_page(403)
        # origem exata da lista; vazia so vale com uma unica origem configurada (formulario antigo)
        configured = browser_service.configured_origins(cfg)
        origem = request.form.get("origem", "")
        if not origem and len(configured) == 1:
            origem = configured[0]
        if origem not in configured:
            return error_page(403)
        try:
            browser_service.request_resume(cfg, origem, clock())
        except browser_service.ResumeRefused:
            return error_page(409)
        except runner.LockBusy:
            return error_page(503)
        return redirect(url_for("index"), code=303)
