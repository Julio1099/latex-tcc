from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
ASSET_DIR = BASE_DIR / "assets"
DB_PATH = Path(os.environ.get("AVALIADOR_DB", BASE_DIR / "avaliador.db"))
HOST = os.environ.get("AVALIADOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("AVALIADOR_PORT", "8080"))
PRIORITY_RECOMMENDATION_SERVICE_ID = "imposto-renda"
PRIORITY_RECOMMENDATION_CONTEXTS = {"pagina-inicial-governo"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists services (
                id text primary key,
                name text not null,
                url text not null,
                important integer not null default 0,
                click_threshold integer not null default 8,
                average_duration_seconds integer not null default 60,
                created_at text not null
            );

            create table if not exists sessions (
                id text primary key,
                user_id text,
                started_at text not null,
                last_seen_at text not null
            );

            create table if not exists click_events (
                id integer primary key autoincrement,
                session_id text not null,
                user_id text,
                page_id text,
                page_type text,
                service_id text,
                url text,
                x integer,
                y integer,
                created_at text not null
            );

            create table if not exists page_time_events (
                id integer primary key autoincrement,
                session_id text not null,
                user_id text,
                page_id text,
                service_id text,
                duration_seconds real not null,
                created_at text not null
            );

            create table if not exists admin_alerts (
                id integer primary key autoincrement,
                session_id text not null,
                service_id text,
                severity text not null,
                reason text not null,
                click_count integer,
                duration_seconds real,
                silent integer not null default 1,
                created_at text not null,
                unique(session_id, service_id, reason)
            );

            create table if not exists feedbacks (
                id integer primary key autoincrement,
                session_id text not null,
                user_id text,
                service_id text,
                stars integer not null,
                nps integer not null,
                traits_json text not null,
                comment text,
                created_at text not null
            );
            """
        )

        seed_service(
            conn,
            service_id="imposto-renda",
            name="Imposto de Renda",
            url="https://www.gov.br/receitafederal/pt-br/assuntos/meu-imposto-de-renda",
            important=True,
            click_threshold=10,
            average_duration_seconds=45,
        )
        seed_service(
            conn,
            service_id="pagina-inicial-governo",
            name="Página inicial do governo",
            url="https://www.gov.br/",
            important=False,
            click_threshold=12,
            average_duration_seconds=60,
        )
        seed_service(
            conn,
            service_id="servico-publico-generico",
            name="Serviço público genérico",
            url="https://www.gov.br/pt-br/servicos",
            important=False,
            click_threshold=12,
            average_duration_seconds=60,
        )


def seed_service(
    conn: sqlite3.Connection,
    service_id: str,
    name: str,
    url: str,
    important: bool,
    click_threshold: int,
    average_duration_seconds: int,
) -> None:
    conn.execute(
        """
        insert into services
            (id, name, url, important, click_threshold, average_duration_seconds, created_at)
        values (?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set
            name = excluded.name,
            url = excluded.url,
            important = excluded.important,
            click_threshold = excluded.click_threshold,
            average_duration_seconds = excluded.average_duration_seconds
        """,
        (
            service_id,
            name,
            url,
            1 if important else 0,
            click_threshold,
            average_duration_seconds,
            now_iso(),
        ),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def ensure_session(conn: sqlite3.Connection, session_id: str, user_id: str | None) -> None:
    current = now_iso()
    conn.execute(
        """
        insert into sessions (id, user_id, started_at, last_seen_at)
        values (?, ?, ?, ?)
        on conflict(id) do update set
            user_id = coalesce(excluded.user_id, sessions.user_id),
            last_seen_at = excluded.last_seen_at
        """,
        (session_id, user_id, current, current),
    )


def get_service(conn: sqlite3.Connection, service_id: str | None) -> dict:
    if service_id:
        row = conn.execute("select * from services where id = ?", (service_id,)).fetchone()
        if row:
            return dict(row)

    return {
        "id": service_id or "servico-desconhecido",
        "name": "Serviço desconhecido",
        "url": "",
        "important": 0,
        "click_threshold": 12,
        "average_duration_seconds": 60,
    }


def get_service_by_id(service_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from services where id = ?", (service_id,)).fetchone()
    return row_to_dict(row)


def count_clicks(conn: sqlite3.Connection, session_id: str, service_id: str | None) -> int:
    row = conn.execute(
        """
        select count(*) as total
        from click_events
        where session_id = ?
          and coalesce(service_id, '') = coalesce(?, '')
        """,
        (session_id, service_id),
    ).fetchone()
    return int(row["total"])


def create_alert_once(
    conn: sqlite3.Connection,
    session_id: str,
    service_id: str | None,
    severity: str,
    reason: str,
    click_count: int | None = None,
    duration_seconds: float | None = None,
) -> None:
    conn.execute(
        """
        insert or ignore into admin_alerts
            (session_id, service_id, severity, reason, click_count, duration_seconds, silent, created_at)
        values (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (session_id, service_id, severity, reason, click_count, duration_seconds, now_iso()),
    )


def feedback_url(session_id: str, service_id: str | None) -> str:
    service = service_id or ""
    return f"/feedback?session_id={session_id}&service_id={service}"


def priority_redirect_action(conn: sqlite3.Connection, reason: str) -> dict:
    service = get_service(conn, PRIORITY_RECOMMENDATION_SERVICE_ID)
    return {
        "type": "suggest_redirect_popup",
        "reason": reason,
        "message": f"Você parece estar procurando {service['name']}. Deseja ir direto para a página do serviço?",
        "target_service_id": service["id"],
        "target_url": service["url"],
    }


def should_recommend_priority_service(service_id: str | None, service: dict) -> bool:
    return int(service.get("important", 0)) == 0 and service_id in PRIORITY_RECOMMENDATION_CONTEXTS


def evaluate_click_actions(
    conn: sqlite3.Connection,
    session_id: str,
    service_id: str | None,
    click_count: int,
) -> list[dict]:
    service = get_service(conn, service_id)
    threshold = int(service["click_threshold"])
    actions: list[dict] = []

    if click_count <= threshold:
        return actions

    create_alert_once(
        conn,
        session_id=session_id,
        service_id=service_id,
        severity="medium",
        reason="clicks_above_threshold",
        click_count=click_count,
    )

    if int(service["important"]) == 1:
        actions.append(
            {
                "type": "suggest_redirect_popup",
                "reason": "high_click_count_on_important_service",
                "message": f"Você parece estar procurando {service['name']}. Deseja ir direto para a página do serviço?",
                "target_service_id": service["id"],
                "target_url": service["url"],
            }
        )

        if click_count >= threshold + 2:
            actions.append(
                {
                    "type": "open_feedback",
                    "reason": "continued_difficulty_after_redirect_suggestion",
                    "url": feedback_url(session_id, service_id),
                }
            )
    elif should_recommend_priority_service(service_id, service):
        actions.append(
            priority_redirect_action(
                conn,
                reason="difficulty_on_government_home_recommend_high_traffic_service",
            )
        )

        if click_count >= threshold + 2:
            actions.append(
                {
                    "type": "open_feedback",
                    "reason": "continued_difficulty_after_priority_recommendation",
                    "url": feedback_url(session_id, service_id),
                }
            )
    else:
        actions.append(
            {
                "type": "open_feedback",
                "reason": "clicks_above_threshold",
                "url": feedback_url(session_id, service_id),
            }
        )

    return actions


def evaluate_duration_actions(
    conn: sqlite3.Connection,
    session_id: str,
    service_id: str | None,
    duration_seconds: float,
) -> list[dict]:
    service = get_service(conn, service_id)
    average = float(service["average_duration_seconds"])
    if duration_seconds <= average * 1.5:
        return []

    create_alert_once(
        conn,
        session_id=session_id,
        service_id=service_id,
        severity="medium",
        reason="duration_above_expected",
        duration_seconds=duration_seconds,
    )

    actions: list[dict] = []
    if should_recommend_priority_service(service_id, service):
        actions.append(
            priority_redirect_action(
                conn,
                reason="high_duration_on_government_home_recommend_high_traffic_service",
            )
        )

    actions.append(
        {
            "type": "open_feedback",
            "reason": "duration_above_expected",
            "url": feedback_url(session_id, service_id),
        }
    )
    return actions


class Handler(BaseHTTPRequestHandler):
    server_version = "AvaliadorServicosPublicos/0.1"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        route = urlparse(self.path)
        try:
            if route.path in ("/", "/index.html"):
                self.handle_home_page()
            elif route.path == "/portal":
                self.handle_portal_page()
            elif route.path.startswith("/servico/"):
                self.handle_service_page(route.path)
            elif route.path.startswith("/assets/"):
                self.handle_asset(route.path)
            elif route.path == "/health":
                self.send_json({"status": "ok", "database": str(DB_PATH)})
            elif route.path == "/api/services":
                self.handle_get_services()
            elif route.path == "/api/admin/alerts":
                self.handle_get_alerts()
            elif route.path == "/api/admin/dashboard":
                self.handle_get_dashboard()
            elif route.path == "/feedback":
                self.handle_feedback_page(route.query)
            else:
                self.send_json({"error": "not_found"}, status=404)
        except Exception as exc:
            self.send_json({"error": "internal_error", "detail": str(exc)}, status=500)

    def do_POST(self) -> None:
        route = urlparse(self.path)
        try:
            if route.path == "/api/events/click":
                self.handle_click()
            elif route.path == "/api/events/page-time":
                self.handle_page_time()
            elif route.path == "/api/feedback":
                self.handle_feedback_submit()
            else:
                self.send_json({"error": "not_found"}, status=404)
        except ValueError as exc:
            self.send_json({"error": "invalid_request", "detail": str(exc)}, status=400)
        except Exception as exc:
            self.send_json({"error": "internal_error", "detail": str(exc)}, status=500)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_click(self) -> None:
        payload = self.read_json()
        session_id = payload.get("session_id") or str(uuid.uuid4())
        user_id = payload.get("user_id")
        service_id = payload.get("service_id")

        with connect() as conn:
            ensure_session(conn, session_id, user_id)
            conn.execute(
                """
                insert into click_events
                    (session_id, user_id, page_id, page_type, service_id, url, x, y, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    payload.get("page_id"),
                    payload.get("page_type"),
                    service_id,
                    payload.get("url"),
                    payload.get("x"),
                    payload.get("y"),
                    now_iso(),
                ),
            )
            click_count = count_clicks(conn, session_id, service_id)
            actions = evaluate_click_actions(conn, session_id, service_id, click_count)

        self.send_json(
            {
                "session_id": session_id,
                "service_id": service_id,
                "click_count_for_service": click_count,
                "actions": actions,
            },
            status=201,
        )

    def handle_page_time(self) -> None:
        payload = self.read_json()
        session_id = payload.get("session_id")
        if not session_id:
            raise ValueError("session_id é obrigatório para registrar tempo de página")

        duration_seconds = float(payload.get("duration_seconds", 0))
        if duration_seconds < 0:
            raise ValueError("duration_seconds não pode ser negativo")

        user_id = payload.get("user_id")
        service_id = payload.get("service_id")

        with connect() as conn:
            ensure_session(conn, session_id, user_id)
            conn.execute(
                """
                insert into page_time_events
                    (session_id, user_id, page_id, service_id, duration_seconds, created_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    payload.get("page_id"),
                    service_id,
                    duration_seconds,
                    now_iso(),
                ),
            )
            actions = evaluate_duration_actions(conn, session_id, service_id, duration_seconds)

        self.send_json(
            {
                "session_id": session_id,
                "service_id": service_id,
                "duration_seconds": duration_seconds,
                "actions": actions,
            },
            status=201,
        )

    def handle_feedback_submit(self) -> None:
        payload = self.read_json()
        session_id = payload.get("session_id")
        if not session_id:
            raise ValueError("session_id é obrigatório")

        stars = int(payload.get("stars", 0))
        nps = int(payload.get("nps", -1))
        traits = payload.get("traits", [])

        if stars < 1 or stars > 5:
            raise ValueError("stars deve estar entre 1 e 5")
        if nps < 0 or nps > 10:
            raise ValueError("nps deve estar entre 0 e 10")
        if not isinstance(traits, list):
            raise ValueError("traits deve ser uma lista")

        with connect() as conn:
            ensure_session(conn, session_id, payload.get("user_id"))
            conn.execute(
                """
                insert into feedbacks
                    (session_id, user_id, service_id, stars, nps, traits_json, comment, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    payload.get("user_id"),
                    payload.get("service_id"),
                    stars,
                    nps,
                    json.dumps(traits, ensure_ascii=False),
                    payload.get("comment"),
                    now_iso(),
                ),
            )

        self.send_json({"status": "accepted"}, status=201)

    def handle_get_services(self) -> None:
        with connect() as conn:
            rows = conn.execute("select * from services order by important desc, name").fetchall()
        self.send_json([dict(row) for row in rows])

    def handle_get_alerts(self) -> None:
        with connect() as conn:
            rows = conn.execute(
                """
                select
                    a.*,
                    s.name as service_name,
                    s.url as service_url
                from admin_alerts a
                left join services s on s.id = a.service_id
                order by a.created_at desc
                limit 100
                """
            ).fetchall()
        self.send_json([dict(row) for row in rows])

    def handle_get_dashboard(self) -> None:
        with connect() as conn:
            service_metrics = conn.execute(
                """
                select
                    coalesce(c.service_id, 'sem-servico') as service_id,
                    coalesce(s.name, 'Sem serviço definido') as service_name,
                    count(*) as total_clicks,
                    count(distinct c.session_id) as affected_sessions
                from click_events c
                left join services s on s.id = c.service_id
                group by coalesce(c.service_id, 'sem-servico'), coalesce(s.name, 'Sem serviço definido')
                order by total_clicks desc
                """
            ).fetchall()

            feedback_metrics = conn.execute(
                """
                select
                    coalesce(service_id, 'sem-servico') as service_id,
                    count(*) as total_feedbacks,
                    round(avg(stars), 2) as average_stars,
                    round(avg(nps), 2) as average_nps
                from feedbacks
                group by coalesce(service_id, 'sem-servico')
                order by total_feedbacks desc
                """
            ).fetchall()

            alert_count = conn.execute("select count(*) as total from admin_alerts").fetchone()

        self.send_json(
            {
                "total_alerts": int(alert_count["total"]),
                "service_metrics": [dict(row) for row in service_metrics],
                "feedback_metrics": [dict(row) for row in feedback_metrics],
            }
        )

    def handle_feedback_page(self, query: str) -> None:
        params = parse_qs(query)
        session_id = params.get("session_id", [""])[0]
        service_id = params.get("service_id", [""])[0]
        if not session_id:
            self.send_html("<h1>session_id ausente</h1>", status=400)
            return

        html = feedback_html(session_id=session_id, service_id=service_id)
        self.send_html(html)

    def handle_home_page(self) -> None:
        self.send_html(home_html())

    def handle_portal_page(self) -> None:
        self.send_html(portal_html())

    def handle_service_page(self, path: str) -> None:
        service_id = path.rsplit("/", 1)[-1] or "imposto-renda"
        service = get_service_by_id(service_id)
        if service is None:
            self.send_json({"error": "service_not_found"}, status=404)
            return
        self.send_html(service_html(service))

    def handle_asset(self, path: str) -> None:
        filename = path.rsplit("/", 1)[-1]
        if not filename or "/" in filename or "\\" in filename:
            self.send_json({"error": "asset_not_found"}, status=404)
            return
        asset = (ASSET_DIR / filename).resolve()
        if ASSET_DIR.resolve() not in asset.parents or not asset.is_file():
            self.send_json({"error": "asset_not_found"}, status=404)
            return
        self.send_file(asset)

    def log_message(self, format: str, *args: object) -> None:
        return


def home_html() -> str:
    return """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Avaliador de Serviços Públicos</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --border: #d7dfeb;
      --text: #172033;
      --muted: #5f6d80;
      --primary: #1f6feb;
      --danger: #b42318;
      --success: #087443;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 22px 28px;
      background: #111827;
      color: #fff;
    }
    header h1 {
      margin: 0 0 6px;
      font-size: 24px;
    }
    header p {
      margin: 0;
      color: #cbd5e1;
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      grid-template-columns: minmax(280px, 420px) 1fr;
      gap: 18px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
    }
    h2 {
      margin: 0 0 14px;
      font-size: 18px;
    }
    label {
      display: block;
      margin: 12px 0 6px;
      font-weight: 700;
    }
    select, input, textarea {
      width: 100%;
      padding: 10px;
      border: 1px solid #b8c4d4;
      border-radius: 6px;
      font: inherit;
    }
    button, a.button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      margin: 8px 8px 0 0;
      padding: 9px 13px;
      border: 0;
      border-radius: 6px;
      background: var(--primary);
      color: #fff;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
    }
    button.secondary, a.secondary { background: #334155; }
    button.ghost, a.ghost {
      background: #fff;
      color: var(--text);
      border: 1px solid var(--border);
    }
    .status {
      display: inline-block;
      padding: 4px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      background: #fee2e2;
      color: var(--danger);
    }
    .status.ok {
      background: #dcfce7;
      color: var(--success);
    }
    .links {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    .metric {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      background: #f8fafc;
    }
    .metric strong {
      display: block;
      font-size: 24px;
      margin-bottom: 4px;
    }
    pre {
      margin: 12px 0 0;
      padding: 12px;
      overflow: auto;
      max-height: 360px;
      background: #0f172a;
      color: #dbeafe;
      border-radius: 8px;
      font-size: 13px;
      white-space: pre-wrap;
    }
    .muted { color: var(--muted); }
    .full { grid-column: 1 / -1; }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; padding: 14px; }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Avaliador de Serviços Públicos</h1>
    <p>Protótipo local para registrar cliques, detectar dificuldade e coletar feedback.</p>
  </header>

  <main>
    <section>
      <h2>Status do servidor</h2>
      <p><span id="server-status" class="status">verificando</span></p>
      <p class="muted" id="database-path"></p>
      <div class="links">
        <a class="button" href="/portal">Abrir protótipo visual</a>
        <a class="button ghost" href="/health">/health</a>
        <a class="button ghost" href="/api/services">/api/services</a>
        <a class="button ghost" href="/api/admin/dashboard">/api/admin/dashboard</a>
        <a class="button ghost" href="/api/admin/alerts">/api/admin/alerts</a>
      </div>
    </section>

    <section>
      <h2>Resumo administrativo</h2>
      <div class="grid">
        <div class="metric">
          <strong id="total-alerts">0</strong>
          <span>Alertas registrados</span>
        </div>
        <div class="metric">
          <strong id="service-count">0</strong>
          <span>Serviços monitorados</span>
        </div>
        <div class="metric">
          <strong id="session-count">0</strong>
          <span>Sessões afetadas</span>
        </div>
      </div>
    </section>

    <section>
      <h2>Simular navegação</h2>
      <label for="service">Serviço</label>
      <select id="service"></select>
      <label for="session">Sessão</label>
      <input id="session" value="sessao-demo-local">
      <button id="click-once">Registrar clique</button>
      <button id="click-many" class="secondary">Gerar 14 cliques</button>
      <button id="time-high" class="secondary">Registrar tempo alto</button>
      <a id="feedback-link" class="button ghost" href="/feedback?session_id=sessao-demo-local&service_id=imposto-renda">Abrir feedback demo</a>
      <pre id="last-response">{}</pre>
    </section>

    <section>
      <h2>Alertas recentes</h2>
      <button id="refresh" class="ghost">Atualizar painel</button>
      <pre id="alerts">[]</pre>
    </section>

    <section class="full">
      <h2>Dashboard bruto</h2>
      <pre id="dashboard">{}</pre>
    </section>
  </main>

  <script>
    const statusEl = document.querySelector("#server-status");
    const databasePath = document.querySelector("#database-path");
    const serviceSelect = document.querySelector("#service");
    const sessionInput = document.querySelector("#session");
    const feedbackLink = document.querySelector("#feedback-link");
    const lastResponse = document.querySelector("#last-response");
    const alertsEl = document.querySelector("#alerts");
    const dashboardEl = document.querySelector("#dashboard");

    function showJson(element, data) {
      element.textContent = JSON.stringify(data, null, 2);
    }

    async function getJson(url) {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`${url} retornou ${response.status}`);
      return response.json();
    }

    async function loadStatus() {
      try {
        const health = await getJson("/health");
        statusEl.textContent = "online";
        statusEl.classList.add("ok");
        databasePath.textContent = `Banco SQLite: ${health.database}`;
      } catch (error) {
        statusEl.textContent = "offline";
        statusEl.classList.remove("ok");
        databasePath.textContent = error.message;
      }
    }

    async function loadServices() {
      const services = await getJson("/api/services");
      serviceSelect.innerHTML = services.map((service) => (
        `<option value="${service.id}">${service.name}</option>`
      )).join("");
      updateFeedbackLink();
      document.querySelector("#service-count").textContent = services.length;
    }

    async function refreshDashboard() {
      const dashboard = await getJson("/api/admin/dashboard");
      const alerts = await getJson("/api/admin/alerts");
      showJson(dashboardEl, dashboard);
      showJson(alertsEl, alerts);
      document.querySelector("#total-alerts").textContent = dashboard.total_alerts ?? 0;
      const sessions = (dashboard.service_metrics || []).reduce(
        (total, item) => total + Number(item.affected_sessions || 0),
        0
      );
      document.querySelector("#session-count").textContent = sessions;
    }

    function updateFeedbackLink() {
      feedbackLink.href = `/feedback?session_id=${encodeURIComponent(sessionInput.value)}&service_id=${encodeURIComponent(serviceSelect.value)}`;
    }

    async function registerClick() {
      updateFeedbackLink();
      const response = await fetch("/api/events/click", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionInput.value,
          page_id: "home-gov-demo",
          page_type: "home",
          service_id: serviceSelect.value,
          url: location.href,
          x: Math.floor(Math.random() * 900),
          y: Math.floor(Math.random() * 700)
        })
      });
      const data = await response.json();
      showJson(lastResponse, data);
      await refreshDashboard();
      return data;
    }

    async function registerManyClicks() {
      let data = {};
      for (let i = 0; i < 14; i += 1) data = await registerClick();
      showJson(lastResponse, data);
    }

    async function registerHighTime() {
      updateFeedbackLink();
      const response = await fetch("/api/events/page-time", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionInput.value,
          service_id: serviceSelect.value,
          page_id: "home-gov-demo",
          duration_seconds: 120
        })
      });
      const data = await response.json();
      showJson(lastResponse, data);
      await refreshDashboard();
    }

    document.querySelector("#click-once").addEventListener("click", registerClick);
    document.querySelector("#click-many").addEventListener("click", registerManyClicks);
    document.querySelector("#time-high").addEventListener("click", registerHighTime);
    document.querySelector("#refresh").addEventListener("click", refreshDashboard);
    serviceSelect.addEventListener("change", updateFeedbackLink);
    sessionInput.addEventListener("input", updateFeedbackLink);

    loadStatus()
      .then(loadServices)
      .then(refreshDashboard)
      .catch((error) => showJson(lastResponse, { error: error.message }));
  </script>
</body>
</html>"""


def prototype_styles() -> str:
    return """
    :root {
      color-scheme: light;
      --blue-900: #071d41;
      --blue-800: #0b2f66;
      --blue-700: #1351a3;
      --blue-600: #1f6feb;
      --green-700: #087443;
      --yellow-500: #f2c94c;
      --red-700: #b42318;
      --ink: #162033;
      --muted: #5d6b82;
      --line: #d7e0ec;
      --soft: #f3f6fb;
      --panel: #ffffff;
      --shadow: 0 10px 28px rgba(13, 31, 61, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: #eef3f9;
      color: var(--ink);
    }
    a { color: inherit; }
    button, input, select {
      font: inherit;
    }
    .prototype-warning {
      background: #fff8df;
      border-bottom: 1px solid #eed779;
      color: #604500;
      padding: 8px 24px;
      font-size: 13px;
      text-align: center;
    }
    .gov-topbar {
      background: var(--blue-900);
      color: #dbeafe;
      padding: 8px 24px;
      font-size: 13px;
    }
    .gov-header {
      background: var(--blue-800);
      color: #fff;
      border-bottom: 4px solid var(--yellow-500);
    }
    .gov-header-inner {
      max-width: 1240px;
      margin: 0 auto;
      padding: 18px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
      text-decoration: none;
    }
    .brand-mark {
      width: 42px;
      height: 42px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: #fff;
      color: var(--blue-800);
      font-weight: 800;
      letter-spacing: 0;
    }
    .brand strong {
      display: block;
      font-size: 20px;
      line-height: 1.1;
    }
    .brand span {
      display: block;
      color: #cbd5e1;
      font-size: 13px;
      margin-top: 3px;
    }
    .nav {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .nav a {
      color: #e5eefc;
      text-decoration: none;
      padding: 9px 11px;
      border-radius: 6px;
    }
    .nav a:hover,
    .nav a:focus {
      background: rgba(255,255,255,0.12);
      outline: none;
    }
    .shell {
      max-width: 1240px;
      margin: 0 auto;
      padding: 22px 24px 34px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 380px;
      gap: 20px;
      align-items: start;
    }
    .content-stack {
      display: grid;
      gap: 18px;
      min-width: 0;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) minmax(260px, 0.85fr);
      overflow: hidden;
    }
    .hero-copy {
      padding: 28px;
    }
    .eyebrow {
      color: var(--blue-700);
      font-weight: 800;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin: 0 0 8px;
    }
    h1 {
      margin: 0;
      font-size: clamp(28px, 4vw, 42px);
      line-height: 1.08;
      letter-spacing: 0;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 20px;
    }
    h3 {
      margin: 0 0 7px;
      font-size: 17px;
    }
    p {
      line-height: 1.55;
    }
    .lead {
      color: var(--muted);
      font-size: 17px;
      max-width: 680px;
    }
    .hero-visual {
      min-height: 280px;
      background: #dce8f7;
      display: flex;
      align-items: stretch;
      justify-content: stretch;
      border-left: 1px solid var(--line);
    }
    .hero-visual img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .search-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      margin-top: 22px;
      max-width: 740px;
    }
    .search-row input {
      border: 1px solid #b8c5d7;
      border-radius: 6px;
      padding: 13px 14px;
      min-width: 0;
    }
    .button,
    button.button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      padding: 11px 15px;
      border: 0;
      border-radius: 6px;
      background: var(--blue-700);
      color: #fff;
      font-weight: 800;
      text-decoration: none;
      cursor: pointer;
      white-space: nowrap;
    }
    .button.secondary {
      background: #334155;
    }
    .button.light {
      background: #fff;
      color: var(--blue-800);
      border: 1px solid var(--line);
    }
    .button.success {
      background: var(--green-700);
    }
    .quick-grid,
    .service-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .card {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      text-decoration: none;
      min-height: 136px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      transition: border-color 0.15s ease, transform 0.15s ease;
    }
    .card:hover,
    .card:focus {
      border-color: var(--blue-600);
      transform: translateY(-1px);
      outline: none;
    }
    .card p {
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }
    .tag-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 5px 9px;
      border-radius: 999px;
      background: #e8f1ff;
      color: var(--blue-800);
      font-size: 13px;
      font-weight: 700;
    }
    .gov-linkbar {
      background: #07152f;
      color: #d7e6ff;
      font-size: 12px;
      border-bottom: 1px solid rgba(255,255,255,0.12);
    }
    .gov-linkbar-inner {
      max-width: 1320px;
      margin: 0 auto;
      padding: 7px 24px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .gov-linkbar a,
    .accessibility-tools button {
      color: #d7e6ff;
      text-decoration: none;
      border: 0;
      background: transparent;
      padding: 0;
      cursor: pointer;
      font-size: 12px;
    }
    .top-link-list,
    .accessibility-tools {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }
    .site-search {
      max-width: 1320px;
      margin: 0 auto;
      padding: 12px 24px;
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 150px;
      gap: 10px;
      background: #fff;
      color: var(--ink);
    }
    .site-search input {
      border: 1px solid #aebdd2;
      border-radius: 4px;
      padding: 11px 12px;
    }
    .mega-nav {
      background: #fff;
      border-bottom: 1px solid var(--line);
      box-shadow: 0 3px 10px rgba(12, 34, 64, 0.06);
    }
    .mega-nav-inner {
      max-width: 1320px;
      margin: 0 auto;
      padding: 0 24px;
      display: grid;
      grid-template-columns: repeat(7, minmax(120px, 1fr));
      gap: 0;
    }
    .menu-group {
      position: relative;
      min-height: 58px;
      padding: 12px 9px;
      border-left: 1px solid #edf1f6;
      cursor: default;
    }
    .menu-group:last-child {
      border-right: 1px solid #edf1f6;
    }
    .menu-title {
      color: var(--blue-800);
      font-weight: 800;
      font-size: 13px;
      line-height: 1.25;
    }
    .submenu {
      margin: 9px 0 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 5px;
      font-size: 12px;
    }
    .submenu a {
      color: #334155;
      text-decoration: none;
    }
    .submenu a:hover,
    .submenu a:focus {
      color: var(--blue-700);
      text-decoration: underline;
      outline: none;
    }
    .portal-dashboard {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 290px;
      gap: 16px;
    }
    .news-feature {
      min-height: 280px;
      background: linear-gradient(135deg, #0b2f66, #1f6feb);
      color: #fff;
      border-radius: 8px;
      padding: 24px;
      display: flex;
      flex-direction: column;
      justify-content: flex-end;
      position: relative;
      overflow: hidden;
    }
    .news-feature::before {
      content: "";
      position: absolute;
      inset: 24px 24px auto auto;
      width: 170px;
      height: 170px;
      border: 20px solid rgba(255,255,255,0.15);
      border-radius: 50%;
    }
    .news-feature h2 {
      max-width: 620px;
      font-size: 28px;
      line-height: 1.12;
      margin: 0 0 8px;
      position: relative;
    }
    .news-feature p {
      max-width: 690px;
      color: #dbeafe;
      margin: 0;
      position: relative;
    }
    .carousel-controls {
      display: flex;
      gap: 6px;
      margin-top: 18px;
      position: relative;
    }
    .carousel-controls button {
      border: 1px solid rgba(255,255,255,0.5);
      background: rgba(255,255,255,0.12);
      color: #fff;
      border-radius: 4px;
      padding: 6px 9px;
    }
    .right-rail {
      display: grid;
      gap: 12px;
    }
    .rail-box {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
    }
    .rail-box h3 {
      font-size: 16px;
      color: var(--blue-800);
    }
    .rail-list,
    .recent-list {
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 8px;
      font-size: 13px;
    }
    .rail-list a,
    .recent-list a {
      color: #1f3b67;
      text-decoration: none;
    }
    .rail-list a:hover,
    .recent-list a:hover {
      text-decoration: underline;
    }
    .service-directory {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .directory-card {
      display: grid;
      gap: 8px;
      min-height: 122px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      text-decoration: none;
    }
    .directory-card strong {
      color: var(--blue-800);
      line-height: 1.25;
    }
    .directory-card span {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
    }
    .highlight-strip {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 8px;
    }
    .highlight-strip a {
      min-height: 74px;
      padding: 10px;
      border-radius: 8px;
      background: #e8f1ff;
      border: 1px solid #c7d9f5;
      color: var(--blue-800);
      text-decoration: none;
      font-size: 13px;
      font-weight: 800;
      display: flex;
      align-items: center;
    }
    .news-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .news-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: #fff;
    }
    .news-thumb {
      height: 92px;
      background: linear-gradient(135deg, #cfe1f7, #edf4ff);
      border-bottom: 1px solid var(--line);
    }
    .news-card div:last-child {
      padding: 12px;
    }
    .news-card a {
      color: var(--blue-800);
      font-weight: 800;
      text-decoration: none;
    }
    .news-card p {
      color: var(--muted);
      font-size: 13px;
      margin: 7px 0 0;
    }
    .utility-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .utility-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 13px;
      background: #f8fbff;
      min-height: 98px;
    }
    .utility-card a {
      color: var(--blue-800);
      font-weight: 800;
      text-decoration: none;
    }
    .calendar-mini {
      display: grid;
      grid-template-columns: repeat(7, 1fr);
      gap: 3px;
      margin-top: 8px;
      font-size: 11px;
      text-align: center;
    }
    .calendar-mini span {
      background: #eef4fd;
      padding: 4px 0;
      border-radius: 3px;
    }
    .confusion-note {
      border-left: 4px solid var(--red-700);
      background: #fff5f5;
      padding: 12px;
      color: #6b1d18;
      border-radius: 6px;
      font-size: 13px;
    }
    .section-body {
      padding: 18px;
    }
    .steps {
      display: grid;
      gap: 10px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .steps li {
      display: grid;
      grid-template-columns: 34px 1fr;
      gap: 10px;
      align-items: start;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f8fbff;
    }
    .step-number {
      width: 30px;
      height: 30px;
      border-radius: 50%;
      background: var(--blue-700);
      color: #fff;
      display: grid;
      place-items: center;
      font-weight: 800;
    }
    .service-layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 280px;
      gap: 16px;
    }
    .service-actions {
      display: grid;
      gap: 10px;
    }
    .service-actions .button {
      width: 100%;
    }
    .breadcrumb {
      color: var(--muted);
      font-size: 14px;
      margin-bottom: 12px;
    }
    .api-console {
      position: sticky;
      top: 16px;
      background: #0f172a;
      color: #dbeafe;
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
      border: 1px solid #263859;
    }
    .api-console header {
      background: #111c34;
      padding: 15px;
      border-bottom: 1px solid #263859;
    }
    .api-console h2 {
      color: #fff;
      margin: 0 0 5px;
      font-size: 18px;
    }
    .api-console p {
      margin: 0;
      color: #b7c7e3;
      font-size: 13px;
    }
    .api-console-body {
      padding: 14px;
      display: grid;
      gap: 12px;
    }
    .api-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid #355073;
      border-radius: 999px;
      color: #cfe0ff;
      padding: 6px 9px;
      font-size: 12px;
      background: rgba(255,255,255,0.04);
    }
    .api-flow {
      display: grid;
      gap: 8px;
    }
    .api-step {
      border-left: 3px solid #38bdf8;
      padding: 8px 10px;
      background: #14223b;
      border-radius: 4px;
      font-size: 13px;
    }
    .api-step strong {
      display: block;
      color: #fff;
      margin-bottom: 3px;
    }
    .api-console button,
    .api-console .button {
      width: 100%;
      margin: 0;
    }
    .api-console pre {
      margin: 0;
      max-height: 270px;
      overflow: auto;
      padding: 10px;
      border-radius: 6px;
      background: #08111f;
      color: #dbeafe;
      border: 1px solid #243956;
      font-size: 12px;
      white-space: pre-wrap;
    }
    .notice {
      display: none;
      border: 1px solid #93c5fd;
      background: #eff6ff;
      color: #1e3a8a;
      border-radius: 8px;
      padding: 12px;
      margin-top: 12px;
    }
    .notice.active {
      display: block;
    }
    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(8, 15, 31, 0.58);
      z-index: 50;
      padding: 18px;
      align-items: center;
      justify-content: center;
    }
    .modal-backdrop.active {
      display: flex;
    }
    .modal {
      width: min(520px, 100%);
      background: #fff;
      border-radius: 8px;
      box-shadow: 0 22px 70px rgba(0,0,0,0.24);
      padding: 22px;
    }
    .modal h2 {
      margin-bottom: 8px;
    }
    .modal-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
      margin-top: 18px;
    }
    @media (max-width: 1000px) {
      .shell,
      .hero,
      .service-layout,
      .portal-dashboard {
        grid-template-columns: 1fr;
      }
      .mega-nav-inner,
      .service-directory,
      .highlight-strip,
      .utility-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .news-grid {
        grid-template-columns: 1fr;
      }
      .api-console {
        position: static;
      }
      .hero-visual {
        border-left: 0;
        border-top: 1px solid var(--line);
      }
    }
    @media (max-width: 720px) {
      .gov-header-inner {
        align-items: flex-start;
        flex-direction: column;
      }
      .nav {
        justify-content: flex-start;
      }
      .shell {
        padding: 14px;
      }
      .quick-grid,
      .service-grid,
      .mega-nav-inner,
      .service-directory,
      .highlight-strip,
      .utility-grid {
        grid-template-columns: 1fr;
      }
      .gov-linkbar-inner,
      .site-search {
        padding-left: 14px;
        padding-right: 14px;
      }
      .site-search {
        grid-template-columns: 1fr;
      }
      .menu-group {
        min-height: auto;
        border-right: 1px solid #edf1f6;
      }
      .search-row {
        grid-template-columns: 1fr;
      }
    }
    """


def prototype_script(page_id: str, page_type: str, service_id: str) -> str:
    return """
  <script>
    const PAGE_ID = "__PAGE_ID__";
    const PAGE_TYPE = "__PAGE_TYPE__";
    const SERVICE_ID = "__SERVICE_ID__";
    const SESSION_KEY = "avaliador_session_id";
    const sessionId = localStorage.getItem(SESSION_KEY) || (
      window.crypto && crypto.randomUUID ? crypto.randomUUID() : `sessao-${Date.now()}`
    );
    localStorage.setItem(SESSION_KEY, sessionId);

    const requestEl = document.querySelector("#api-request");
    const responseEl = document.querySelector("#api-response");
    const actionEl = document.querySelector("#api-action");
    const sessionEl = document.querySelector("#api-session");
    const feedbackNotice = document.querySelector("#feedback-notice");
    const feedbackNoticeLink = document.querySelector("#feedback-notice-link");
    const modal = document.querySelector("#redirect-modal");
    const modalMessage = document.querySelector("#redirect-message");
    const redirectButton = document.querySelector("#redirect-accept");
    const dismissButton = document.querySelector("#redirect-dismiss");
    let pendingRedirectService = "";

    if (sessionEl) sessionEl.textContent = sessionId;

    function showJson(element, data) {
      if (element) element.textContent = JSON.stringify(data, null, 2);
    }

    function localServiceUrl(serviceId) {
      return `/servico/${encodeURIComponent(serviceId || "imposto-renda")}`;
    }

    function setAction(text) {
      if (actionEl) actionEl.textContent = text;
    }

    function showFeedback(url) {
      if (!feedbackNotice || !feedbackNoticeLink) return;
      feedbackNotice.classList.add("active");
      feedbackNoticeLink.href = url;
    }

    function showRedirectPopup(action) {
      if (!modal) return;
      pendingRedirectService = action.target_service_id || SERVICE_ID;
      modalMessage.textContent = action.message || "Deseja ir direto para o serviço indicado?";
      modal.classList.add("active");
    }

    async function handleActions(actions) {
      if (!actions || actions.length === 0) {
        setAction("Nenhuma ação. A navegação segue normalmente.");
        return;
      }
      setAction(actions.map((action) => `${action.type}: ${action.reason}`).join("\\n"));
      for (const action of actions) {
        if (action.type === "suggest_redirect_popup") showRedirectPopup(action);
        if (action.type === "open_feedback") showFeedback(action.url);
      }
    }

    async function postJson(url, payload) {
      showJson(requestEl, { method: "POST", url, body: payload });
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      showJson(responseEl, data);
      await handleActions(data.actions || []);
      return data;
    }

    async function registerClick(event, label) {
      const payload = {
        session_id: sessionId,
        page_id: PAGE_ID,
        page_type: PAGE_TYPE,
        service_id: SERVICE_ID,
        url: location.href,
        x: event ? Math.round(event.clientX) : 0,
        y: event ? Math.round(event.clientY) : 0,
        label: label || "click"
      };
      return postJson("/api/events/click", payload);
    }

    async function registerPageTime(seconds) {
      const payload = {
        session_id: sessionId,
        page_id: PAGE_ID,
        service_id: SERVICE_ID,
        duration_seconds: seconds
      };
      return postJson("/api/events/page-time", payload);
    }

    document.addEventListener("click", async (event) => {
      const target = event.target.closest("[data-track]");
      if (!target) return;
      const label = target.getAttribute("data-track");
      const href = target.tagName === "A" ? target.getAttribute("href") : "";
      const shouldNavigate = href && !href.startsWith("#") && target.getAttribute("target") !== "_blank";

      if (shouldNavigate) event.preventDefault();
      await registerClick(event, label);
      if (shouldNavigate) window.location.href = href;
    });

    document.querySelector("#simulate-clicks")?.addEventListener("click", async () => {
      let data = {};
      for (let index = 0; index < 14; index += 1) {
        data = await registerClick(null, `simulacao-${index + 1}`);
      }
      showJson(responseEl, data);
    });

    document.querySelector("#simulate-time")?.addEventListener("click", async () => {
      await registerPageTime(120);
    });

    redirectButton?.addEventListener("click", () => {
      window.location.href = localServiceUrl(pendingRedirectService);
    });

    dismissButton?.addEventListener("click", () => {
      modal.classList.remove("active");
      setAction("Usuário recusou o redirecionamento sugerido.");
    });

    showJson(requestEl, {
      session_id: sessionId,
      page_id: PAGE_ID,
      page_type: PAGE_TYPE,
      service_id: SERVICE_ID,
      status: "aguardando interação"
    });
    showJson(responseEl, {
      mensagem: "Clique em elementos do protótipo ou use as simulações para ver a API funcionando."
    });
    setAction("Aguardando cliques ou tempo acima da média.");
  </script>
    """.replace("__PAGE_ID__", page_id).replace("__PAGE_TYPE__", page_type).replace("__SERVICE_ID__", service_id)


def api_console_html() -> str:
    return """
    <aside class="api-console" aria-label="API em tempo real">
      <header>
        <h2>API em tempo real</h2>
        <p>Mostra como o protótipo conversa com o backend durante a navegação.</p>
      </header>
      <div class="api-console-body">
        <span class="api-pill">Sessão: <strong id="api-session"></strong></span>
        <div class="api-flow">
          <div class="api-step"><strong>1. Captura</strong>O clique ou tempo de navegação é registrado no frontend.</div>
          <div class="api-step"><strong>2. Envio</strong>O evento é enviado para `/api/events/click` ou `/api/events/page-time`.</div>
          <div class="api-step"><strong>3. Decisão</strong>A API compara com limites e devolve ações: popup, feedback ou fluxo normal.</div>
        </div>
        <button id="simulate-clicks" class="button">Simular excesso de cliques</button>
        <button id="simulate-time" class="button secondary">Simular tempo alto</button>
        <a class="button light" href="/">Abrir painel admin</a>
        <div>
          <h3>Última requisição</h3>
          <pre id="api-request">{}</pre>
        </div>
        <div>
          <h3>Última resposta</h3>
          <pre id="api-response">{}</pre>
        </div>
        <div>
          <h3>Ação da API</h3>
          <pre id="api-action"></pre>
        </div>
      </div>
    </aside>
    """


def prototype_modal_html() -> str:
    return """
    <div id="redirect-modal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="redirect-title">
      <div class="modal">
        <h2 id="redirect-title">Sugestão de redirecionamento</h2>
        <p id="redirect-message"></p>
        <p class="muted">No sistema real, este popup apareceria apenas para serviços marcados como importantes ou de alto tráfego.</p>
        <div class="modal-actions">
          <button id="redirect-dismiss" class="button light">Continuar aqui</button>
          <button id="redirect-accept" class="button success">Ir para o serviço</button>
        </div>
      </div>
    </div>
    """


def portal_html() -> str:
    template = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Portal Cidadão - Protótipo</title>
  <style>__STYLES__</style>
</head>
<body>
  <div class="prototype-warning">Protótipo acadêmico. Esta página não é um serviço oficial do governo.</div>
  <div class="gov-linkbar">
    <div class="gov-linkbar-inner">
      <div class="top-link-list">
        <a href="#sobre-governo" data-track="topo-sobre-governo">Sobre o Governo</a>
        <a href="#etica" data-track="topo-codigo-etica">Código de Ética</a>
        <a href="#transparencia" data-track="topo-transparencia">Transparência</a>
        <a href="#ouvidoria" data-track="topo-ouvidoria">Ouvidoria</a>
        <a href="#acesso-informacao" data-track="topo-acesso-informacao">Acesso à Informação</a>
        <a href="#diario" data-track="topo-diario-oficial">Diário Oficial</a>
        <a href="#dados-abertos" data-track="topo-dados-abertos">Dados Abertos</a>
        <a href="#lgpd" data-track="topo-lgpd">LGPD</a>
      </div>
      <div class="accessibility-tools" aria-label="Ferramentas de acessibilidade">
        <button data-track="fonte-menor">A-</button>
        <button data-track="fonte-maior">A+</button>
        <button data-track="alto-contraste">Alto contraste</button>
        <button data-track="vlibras">VLibras</button>
        <button data-track="entrar">Entrar</button>
      </div>
    </div>
  </div>
  <header class="gov-header">
    <div class="gov-header-inner">
      <a class="brand" href="/portal" data-track="voltar-portal">
        <span class="brand-mark">DF</span>
        <span><strong>Secretaria de Serviços ao Cidadão</strong><span>Portal de informações, programas e atendimento</span></span>
      </a>
      <nav class="nav" aria-label="Navegação principal">
        <a href="#fale" data-track="atalho-fale-secretaria">Fale com a Secretaria</a>
        <a href="#noticias" data-track="atalho-noticias">Notícias</a>
        <a href="#servicos" data-track="atalho-servicos">Carta de Serviços</a>
        <a href="/" data-track="atalho-admin">Painel admin</a>
      </nav>
    </div>
  </header>
  <div class="site-search" role="search">
    <input aria-label="Barra de busca" placeholder="Buscar no portal: serviço, edital, unidade, programa, protocolo..." data-track="barra-busca-geral">
    <button class="button" data-track="executar-busca-geral">Buscar</button>
  </div>
  <nav class="mega-nav" aria-label="Menu principal com submenus">
    <div class="mega-nav-inner">
      <div class="menu-group">
        <div class="menu-title">A Secretaria</div>
        <ul class="submenu">
          <li><a href="#perfil" data-track="menu-perfil-secretario">Perfil do Secretário</a></li>
          <li><a href="#agenda" data-track="menu-agenda-secretario">Agenda</a></li>
          <li><a href="#quem-e-quem" data-track="menu-quem-e-quem">Quem é Quem</a></li>
          <li><a href="#regimento" data-track="menu-regimento">Regimento Interno</a></li>
        </ul>
      </div>
      <div class="menu-group">
        <div class="menu-title">Programas e Projetos</div>
        <ul class="submenu">
          <li><a href="/servico/servico-publico-generico" data-track="menu-todos-programas">Todos os programas</a></li>
          <li><a href="#cidadao" data-track="menu-cidadao-digital">Cidadão Digital</a></li>
          <li><a href="#bolsa" data-track="menu-bolsa-atendimento">Bolsa Atendimento</a></li>
          <li><a href="#parcerias" data-track="menu-parcerias">Parcerias</a></li>
        </ul>
      </div>
      <div class="menu-group">
        <div class="menu-title">Unidades e Espaços</div>
        <ul class="submenu">
          <li><a href="#unidades" data-track="menu-todas-unidades">Todas as unidades</a></li>
          <li><a href="#agendamento" data-track="menu-agendamento">Agendamento presencial</a></li>
          <li><a href="#postos" data-track="menu-postos">Postos de atendimento</a></li>
          <li><a href="#mapa" data-track="menu-mapa">Mapa de serviços</a></li>
        </ul>
      </div>
      <div class="menu-group">
        <div class="menu-title">Transparência</div>
        <ul class="submenu">
          <li><a href="#acoes" data-track="menu-acoes-programas">Ações e Programas</a></li>
          <li><a href="#auditorias" data-track="menu-auditorias">Auditorias</a></li>
          <li><a href="#despesas" data-track="menu-despesas">Despesas</a></li>
          <li><a href="#licitacoes" data-track="menu-licitacoes">Licitações</a></li>
          <li><a href="#contratos" data-track="menu-contratos">Contratos</a></li>
        </ul>
      </div>
      <div class="menu-group">
        <div class="menu-title">Comunicação</div>
        <ul class="submenu">
          <li><a href="#noticias" data-track="menu-noticias">Notícias</a></li>
          <li><a href="#imprensa" data-track="menu-imprensa">Assessoria de imprensa</a></li>
          <li><a href="#publicacoes" data-track="menu-publicacoes">Publicações</a></li>
          <li><a href="#eprotocolo" data-track="menu-eprotocolo">E-protocolo</a></li>
        </ul>
      </div>
      <div class="menu-group">
        <div class="menu-title">Governança</div>
        <ul class="submenu">
          <li><a href="#comite" data-track="menu-comite">Comitê Interno</a></li>
          <li><a href="#plano" data-track="menu-plano-estrategico">Plano Estratégico</a></li>
          <li><a href="#riscos" data-track="menu-riscos">Gestão de Riscos</a></li>
          <li><a href="#servidor" data-track="menu-servidor">Portal do Servidor</a></li>
        </ul>
      </div>
      <div class="menu-group">
        <div class="menu-title">Serviços rápidos</div>
        <ul class="submenu">
          <li><a href="/servico/imposto-renda" data-track="menu-imposto-renda">Imposto de Renda</a></li>
          <li><a href="/servico/pagina-inicial-governo" data-track="menu-pagina-inicial">Página inicial</a></li>
          <li><a href="#ouvidoria-card" data-track="menu-ouvidoria-card">Ouvidoria</a></li>
          <li><a href="#sic" data-track="menu-sic">Consultar SIC</a></li>
        </ul>
      </div>
    </div>
  </nav>

  <main class="shell">
    <div class="content-stack">
      <section class="panel">
        <div class="section-body portal-dashboard">
          <div class="news-feature">
            <p class="eyebrow" style="color:#fff;">Publicador de conteúdos e mídias</p>
            <h2>Portal reúne programas, notícias, editais e serviços em uma mesma página</h2>
            <p>Na prática, o cidadão precisa decidir entre menus institucionais, destaques, cartões, notícias, serviços e links externos antes de encontrar o caminho correto.</p>
            <div class="carousel-controls">
              <button data-track="carrossel-anterior">Anterior</button>
              <button data-track="carrossel-proximo">Próximo</button>
              <button data-track="carrossel-noticia-principal">Saiba mais</button>
            </div>
          </div>
          <aside class="right-rail">
            <div class="rail-box">
              <h3>Acesso rápido</h3>
              <ul class="rail-list">
                <li><a href="/servico/imposto-renda" data-track="rail-imposto-renda">Imposto de Renda</a></li>
                <li><a href="/servico/servico-publico-generico" data-track="rail-servico-generico">Solicitar atendimento</a></li>
                <li><a href="#agendamento" data-track="rail-agendamento">Agendamento</a></li>
                <li><a href="#protocolo" data-track="rail-protocolo">Consultar protocolo</a></li>
                <li><a href="#documentos" data-track="rail-documentos">Documentos necessários</a></li>
              </ul>
            </div>
            <div class="rail-box">
              <h3>Diário Oficial</h3>
              <p class="muted" style="font-size:13px;">Ou selecione uma data</p>
              <div class="calendar-mini">
                <span>D</span><span>S</span><span>T</span><span>Q</span><span>Q</span><span>S</span><span>S</span>
                <span>1</span><span>2</span><span>3</span><span>4</span><span>5</span><span>6</span><span>7</span>
                <span>8</span><span>9</span><span>10</span><span>11</span><span>12</span><span>13</span><span>14</span>
              </div>
            </div>
            <div class="confusion-note">
              Esta página simula um cenário realista de excesso de caminhos. Use a simulação de cliques para demonstrar a detecção de dificuldade.
            </div>
          </aside>
        </div>
      </section>

      <section id="servicos" class="panel">
        <div class="section-body">
          <h2>Destaques do portal</h2>
          <div class="highlight-strip">
            <a href="/servico/imposto-renda" data-track="destaque-imposto-renda">Imposto de Renda</a>
            <a href="#carta" data-track="destaque-carta-servicos">Carta de Serviços</a>
            <a href="#consulta" data-track="destaque-consulta-publica">Consulta pública</a>
            <a href="#editais" data-track="destaque-editais">Editais</a>
            <a href="#transparencia" data-track="destaque-transparencia">Transparência</a>
            <a href="#ouvidoria" data-track="destaque-ouvidoria">Ouvidoria</a>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="section-body">
          <h2>Carta de Serviços</h2>
          <div class="service-directory">
            <a class="directory-card" href="/servico/imposto-renda" data-track="servico-imposto-renda">
              <strong>Imposto de Renda</strong>
              <span>Serviço prioritário para demonstrar popup e redirecionamento.</span>
            </a>
            <a class="directory-card" href="/servico/servico-publico-generico" data-track="servico-atendimento-social">
              <strong>Atendimento social</strong>
              <span>Solicitações, análise de dados e acompanhamento de atendimento.</span>
            </a>
            <a class="directory-card" href="#licenciamento" data-track="servico-licenciamento">
              <strong>Licenciamento e autorizações</strong>
              <span>Orientações, documentos, formulários e protocolos.</span>
            </a>
            <a class="directory-card" href="#agendamento" data-track="servico-agendamento">
              <strong>Agendar atendimento</strong>
              <span>Unidades, datas, horários e confirmação de presença.</span>
            </a>
            <a class="directory-card" href="#beneficios" data-track="servico-beneficios">
              <strong>Benefícios e programas</strong>
              <span>Inscrições, editais, prazos e critérios de participação.</span>
            </a>
            <a class="directory-card" href="#protocolo" data-track="servico-protocolo">
              <strong>E-protocolo</strong>
              <span>Registro e acompanhamento de demandas administrativas.</span>
            </a>
            <a class="directory-card" href="#documentos" data-track="servico-documentos">
              <strong>Documentos e certidões</strong>
              <span>Emissão, validação e segunda via de documentos.</span>
            </a>
            <a class="directory-card" href="#perguntas" data-track="servico-perguntas">
              <strong>Perguntas frequentes</strong>
              <span>Respostas espalhadas por temas parecidos e páginas relacionadas.</span>
            </a>
          </div>
        </div>
      </section>

      <section id="noticias" class="panel">
        <div class="section-body">
          <h2>Notícias e publicações recentes</h2>
          <div class="news-grid">
            <article class="news-card">
              <div class="news-thumb"></div>
              <div><a href="#noticia-1" data-track="noticia-resultado-edital">Resultado definitivo de chamamento público</a><p>Comissão divulga julgamento de propostas e orientações para recursos.</p></div>
            </article>
            <article class="news-card">
              <div class="news-thumb"></div>
              <div><a href="#noticia-2" data-track="noticia-seminario">Inscrições abertas para seminário</a><p>Evento intersetorial com certificado e transmissão ao vivo.</p></div>
            </article>
            <article class="news-card">
              <div class="news-thumb"></div>
              <div><a href="#noticia-3" data-track="noticia-consulta-publica">Consulta pública debate regras de atendimento</a><p>A iniciativa recebe contribuições da população por formulário eletrônico.</p></div>
            </article>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="section-body">
          <h2>Aplicações aninhadas</h2>
          <div class="utility-grid">
            <div class="utility-card" id="acesso-informacao"><a href="#lai" data-track="util-acesso-informacao">Acesso à informação</a><p class="muted">Pedidos, recursos e relatórios.</p></div>
            <div class="utility-card" id="ouvidoria-card"><a href="#ouvidoria" data-track="util-ouvidoria">Ouvidoria</a><p class="muted">Reclamações, elogios e denúncias.</p></div>
            <div class="utility-card"><a href="#sic" data-track="util-consultar-sic">Consultar SIC</a><p class="muted">Acompanhe solicitação anterior.</p></div>
            <div class="utility-card"><a href="#legislacao" data-track="util-legislacao">Legislação</a><p class="muted">Normas, decretos e portarias.</p></div>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="section-body portal-dashboard">
          <div>
            <h2>Conteúdo recente</h2>
            <ul class="recent-list">
              <li><a href="#recente-1" data-track="recente-edital">Edital de chamamento público nº 01/2026</a></li>
              <li><a href="#recente-2" data-track="recente-parceria">Parceria fortalece atendimento em unidades regionais</a></li>
              <li><a href="#recente-3" data-track="recente-calendario">Calendário de inscrições e prazos administrativos</a></li>
              <li><a href="#recente-4" data-track="recente-lista">Lista de documentos obrigatórios para solicitação</a></li>
              <li><a href="#recente-5" data-track="recente-perguntas">Perguntas frequentes atualizadas</a></li>
            </ul>
          </div>
          <aside class="rail-box">
            <h3>Redes e canais</h3>
            <ul class="rail-list">
              <li><a href="#instagram" data-track="canal-instagram">Instagram</a></li>
              <li><a href="#youtube" data-track="canal-youtube">YouTube</a></li>
              <li><a href="#facebook" data-track="canal-facebook">Facebook</a></li>
              <li><a href="#flickr" data-track="canal-flickr">Flickr</a></li>
            </ul>
          </aside>
        </div>
      </section>

      <section class="panel">
        <div class="section-body">
          <h2>O que torna esta tela mais realista para o TCC</h2>
          <div class="quick-grid">
            <div class="card">
              <h3>Muitos caminhos similares</h3>
              <p>Menus, destaques, cartões e notícias oferecem rotas concorrentes para tarefas parecidas.</p>
            </div>
            <div class="card">
              <h3>Links institucionais misturados</h3>
              <p>Transparência, ouvidoria, diário oficial e serviços aparecem no mesmo fluxo visual.</p>
            </div>
            <div class="card">
              <h3>Ruído suficiente para medir dificuldade</h3>
              <p>A API consegue registrar tentativas repetidas antes de sugerir feedback ou redirecionamento.</p>
            </div>
          </div>
        </div>
      </section>
    </div>

    __API_CONSOLE__
  </main>

  <div id="feedback-notice" class="notice" style="position:fixed;left:24px;bottom:24px;z-index:40;max-width:420px;">
    A API detectou dificuldade nesta sessão.
    <a id="feedback-notice-link" class="button" href="/feedback?session_id=&service_id=pagina-inicial-governo">Abrir feedback</a>
  </div>

  __MODAL__
  __SCRIPT__
</body>
</html>"""
    return (
        template.replace("__STYLES__", prototype_styles())
        .replace("__API_CONSOLE__", api_console_html())
        .replace("__MODAL__", prototype_modal_html())
        .replace("__SCRIPT__", prototype_script("portal-cidadao", "home", "pagina-inicial-governo"))
    )


def service_html(service: dict) -> str:
    service_id = str(service["id"])
    service_name = str(service["name"])
    is_important = int(service.get("important", 0)) == 1
    priority_label = "Serviço prioritário" if is_important else "Serviço comum"
    threshold = int(service.get("click_threshold", 8))
    average_duration = int(service.get("average_duration_seconds", 60))

    template = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__SERVICE_NAME__ - Protótipo</title>
  <style>__STYLES__</style>
</head>
<body>
  <div class="prototype-warning">Protótipo acadêmico. Esta página não é um serviço oficial do governo.</div>
  <div class="gov-topbar">Ambiente de demonstração para TCC - Avaliador de Serviços Públicos</div>
  <header class="gov-header">
    <div class="gov-header-inner">
      <a class="brand" href="/portal" data-track="voltar-portal">
        <span class="brand-mark">PC</span>
        <span><strong>Portal Cidadão</strong><span>Serviços públicos digitais</span></span>
      </a>
      <nav class="nav" aria-label="Navegação principal">
        <a href="/portal" data-track="menu-inicio">Início</a>
        <a href="/" data-track="menu-admin">Painel admin</a>
      </nav>
    </div>
  </header>

  <main class="shell">
    <div class="content-stack">
      <section class="panel">
        <div class="section-body">
          <div class="breadcrumb">Início / Serviços / __SERVICE_NAME__</div>
          <p class="eyebrow">__PRIORITY_LABEL__</p>
          <h1>__SERVICE_NAME__</h1>
          <p class="lead">Página simulada de serviço público. Use os botões e links abaixo para gerar eventos e ver como a API responde em tempo real.</p>
          <div class="tag-row">
            <span class="tag">Limite de cliques: __THRESHOLD__</span>
            <span class="tag">Tempo médio: __AVERAGE_DURATION__s</span>
            <span class="tag">service_id: __SERVICE_ID__</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="section-body service-layout">
          <div>
            <h2>Etapas do serviço</h2>
            <ol class="steps">
              <li><span class="step-number">1</span><span><strong>Identificação</strong><br>Informe CPF, dados pessoais ou certificado digital.</span></li>
              <li><span class="step-number">2</span><span><strong>Consulta</strong><br>Confira pendências, prazos, comprovantes e orientações.</span></li>
              <li><span class="step-number">3</span><span><strong>Solicitação</strong><br>Envie o pedido ou avance para o sistema responsável pelo atendimento.</span></li>
              <li><span class="step-number">4</span><span><strong>Acompanhamento</strong><br>Receba protocolo, status e próximos passos.</span></li>
            </ol>
          </div>
          <aside class="service-actions">
            <h2>Ações</h2>
            <button class="button" data-track="iniciar-servico">Iniciar serviço</button>
            <button class="button light" data-track="consultar-status">Consultar status</button>
            <button class="button light" data-track="ver-documentos">Ver documentos necessários</button>
            <button class="button light" data-track="abrir-ajuda">Ajuda e perguntas frequentes</button>
            <a class="button secondary" href="/portal" data-track="voltar-busca">Voltar para busca</a>
          </aside>
        </div>
      </section>

      <section class="panel">
        <div class="section-body">
          <h2>Como a API atua nesta tela</h2>
          <div class="quick-grid">
            <div class="card">
              <h3>Registro silencioso</h3>
              <p>Cada clique envia sessão, página, serviço, URL e posição do cursor.</p>
            </div>
            <div class="card">
              <h3>Regra de decisão</h3>
              <p>Ao ultrapassar o limite, a API cria alerta para o administrador.</p>
            </div>
            <div class="card">
              <h3>Ação contextual</h3>
              <p>Serviços prioritários podem exibir popup; dificuldades persistentes abrem feedback.</p>
            </div>
          </div>
        </div>
      </section>
    </div>

    __API_CONSOLE__
  </main>

  <div id="feedback-notice" class="notice" style="position:fixed;left:24px;bottom:24px;z-index:40;max-width:420px;">
    A API detectou dificuldade nesta sessão.
    <a id="feedback-notice-link" class="button" href="/feedback?session_id=&service_id=__SERVICE_ID__">Abrir feedback</a>
  </div>

  __MODAL__
  __SCRIPT__
</body>
</html>"""
    return (
        template.replace("__STYLES__", prototype_styles())
        .replace("__SERVICE_ID__", service_id)
        .replace("__SERVICE_NAME__", service_name)
        .replace("__PRIORITY_LABEL__", priority_label)
        .replace("__THRESHOLD__", str(threshold))
        .replace("__AVERAGE_DURATION__", str(average_duration))
        .replace("__API_CONSOLE__", api_console_html())
        .replace("__MODAL__", prototype_modal_html())
        .replace("__SCRIPT__", prototype_script(f"servico-{service_id}", "service", service_id))
    )


def feedback_html(session_id: str, service_id: str) -> str:
    traits = [
        ("clareza_informacoes", "Informações confusas"),
        ("tempo_obtencao", "Demorou demais"),
        ("esforco_alto", "Exigiu muitos passos"),
        ("usabilidade", "Difícil de usar"),
        ("atendimento", "Atendimento insuficiente"),
        ("eficacia", "Não resolveu minha necessidade"),
    ]
    trait_inputs = "\n".join(
        f'<label><input type="checkbox" name="traits" value="{value}"> {label}</label>'
        for value, label in traits
    )
    star_inputs = "\n".join(
        f"""
          <label class="rating-option" data-rating="{value}" aria-label="{value} de 5 - {label}">
            <input type="radio" name="stars" value="{value}">
            <span class="rating-star">★</span>
            <span class="rating-text">{label}</span>
          </label>
        """
        for value, label in [
            (1, "Péssima"),
            (2, "Ruim"),
            (3, "Ok"),
            (4, "Boa"),
            (5, "Excelente"),
        ]
    )

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedback do serviço público</title>
  <style>
    body {{
      font-family: Arial, sans-serif;
      margin: 0;
      background: #f5f7fb;
      color: #18202f;
    }}
    main {{
      max-width: 680px;
      margin: 48px auto;
      padding: 28px;
      background: #fff;
      border: 1px solid #d9e0eb;
      border-radius: 8px;
    }}
    h1 {{ font-size: 24px; margin: 0 0 18px; }}
    fieldset {{ border: 0; padding: 0; margin: 22px 0; }}
    legend {{ font-weight: 700; margin-bottom: 10px; }}
    .rating-field {{
      margin-top: 18px;
    }}
    .rating-heading {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
    }}
    .rating-caption {{
      color: #64748b;
      font-size: 14px;
      font-weight: 700;
    }}
    .rating-options {{
      display: grid;
      grid-template-columns: repeat(5, minmax(70px, 1fr));
      gap: 10px;
      max-width: 520px;
    }}
    .rating-option {{
      position: relative;
      min-height: 88px;
      padding: 12px 8px 10px;
      border: 1px solid #d4dbe7;
      border-radius: 8px;
      background: #fff;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 5px;
      transition: border-color 0.15s ease, background 0.15s ease, transform 0.15s ease;
    }}
    .rating-option input {{
      position: absolute;
      inset: 0;
      opacity: 0;
      cursor: pointer;
    }}
    .rating-star {{
      color: #cbd5e1;
      font-size: 38px;
      line-height: 1;
      transition: color 0.15s ease, transform 0.15s ease;
    }}
    .rating-text {{
      color: #64748b;
      font-size: 12px;
      font-weight: 700;
    }}
    .rating-option.active,
    .rating-option.preview {{
      border-color: #f6b300;
      background: #fff8e1;
    }}
    .rating-option.active .rating-star,
    .rating-option.preview .rating-star {{
      color: #f6b300;
      transform: scale(1.05);
    }}
    .rating-option.active .rating-text,
    .rating-option.preview .rating-text {{
      color: #7a4c00;
    }}
    .rating-option:focus-within {{
      outline: 3px solid rgba(31, 111, 235, 0.25);
      outline-offset: 2px;
    }}
    .rating-error {{
      display: none;
      color: #b42318;
      font-size: 14px;
      margin: 10px 0 0;
      font-weight: 700;
    }}
    .rating-error.active {{
      display: block;
    }}
    .traits label {{ display: block; margin: 8px 0; }}
    textarea, select {{
      width: 100%;
      box-sizing: border-box;
      padding: 10px;
      border: 1px solid #b9c3d1;
      border-radius: 6px;
    }}
    button {{
      padding: 12px 18px;
      border: 0;
      border-radius: 6px;
      background: #1f6feb;
      color: #fff;
      font-weight: 700;
      cursor: pointer;
    }}
    #status {{ margin-top: 16px; font-weight: 700; }}
    @media (max-width: 560px) {{
      main {{
        margin: 0;
        min-height: 100vh;
        border: 0;
        border-radius: 0;
      }}
      .rating-options {{
        grid-template-columns: repeat(5, minmax(48px, 1fr));
        gap: 6px;
      }}
      .rating-option {{
        min-height: 74px;
        padding: 8px 4px;
      }}
      .rating-star {{
        font-size: 31px;
      }}
      .rating-text {{
        font-size: 10px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Como foi sua experiência?</h1>
    <form id="feedback-form">
      <fieldset class="rating-field">
        <div class="rating-heading">
          <legend>Avaliação geral</legend>
          <span id="rating-caption" class="rating-caption">Toque em uma nota</span>
        </div>
        <div class="rating-options" role="radiogroup" aria-label="Avaliação geral">
          {star_inputs}
        </div>
        <p id="rating-error" class="rating-error">Escolha uma nota para continuar.</p>
      </fieldset>

      <fieldset class="traits">
        <legend>O que pode melhorar?</legend>
        {trait_inputs}
      </fieldset>

      <fieldset>
        <legend>NPS</legend>
        <label for="nps">Qual a chance de você recomendar este serviço? (0 a 10)</label>
        <select id="nps" name="nps" required>
          <option value="">Selecione</option>
          {"".join(f'<option value="{i}">{i}</option>' for i in range(11))}
        </select>
      </fieldset>

      <fieldset>
        <legend>Comentário opcional</legend>
        <textarea name="comment" rows="4" placeholder="Descreva brevemente o problema encontrado"></textarea>
      </fieldset>

      <button type="submit">Enviar feedback</button>
      <p id="status"></p>
    </form>
  </main>

  <script>
    const form = document.querySelector("#feedback-form");
    const status = document.querySelector("#status");
    const ratingOptions = Array.from(document.querySelectorAll(".rating-option"));
    const ratingCaption = document.querySelector("#rating-caption");
    const ratingError = document.querySelector("#rating-error");
    const ratingLabels = {{
      1: "Péssima",
      2: "Ruim",
      3: "Ok",
      4: "Boa",
      5: "Excelente"
    }};

    function paintRating(value, mode = "active") {{
      ratingOptions.forEach((option) => {{
        const rating = Number(option.dataset.rating);
        option.classList.toggle(mode, rating <= value);
      }});
    }}

    function clearPreview() {{
      ratingOptions.forEach((option) => option.classList.remove("preview"));
    }}

    function selectedRating() {{
      return Number(form.querySelector('input[name="stars"]:checked')?.value || 0);
    }}

    ratingOptions.forEach((option) => {{
      const input = option.querySelector("input");
      option.addEventListener("mouseenter", () => {{
        clearPreview();
        paintRating(Number(option.dataset.rating), "preview");
      }});
      option.addEventListener("mouseleave", () => {{
        clearPreview();
      }});
      input.addEventListener("change", () => {{
        const value = Number(input.value);
        ratingOptions.forEach((item) => item.classList.remove("active"));
        paintRating(value);
        ratingCaption.textContent = ratingLabels[value];
        ratingError.classList.remove("active");
      }});
    }});

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const data = new FormData(form);
      if (!selectedRating()) {{
        ratingError.classList.add("active");
        status.textContent = "";
        return;
      }}
      const payload = {{
        session_id: "{session_id}",
        service_id: "{service_id}",
        stars: Number(data.get("stars")),
        nps: Number(data.get("nps")),
        traits: data.getAll("traits"),
        comment: data.get("comment")
      }};

      const response = await fetch("/api/feedback", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload)
      }});

      status.textContent = response.ok
        ? "Feedback registrado. Obrigado."
        : "Não foi possível registrar o feedback.";
    }});
  </script>
</body>
</html>"""


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Avaliador de Serviços Públicos API em http://{HOST}:{PORT}")
    print(f"Banco SQLite: {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
