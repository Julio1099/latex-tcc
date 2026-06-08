from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("AVALIADOR_DB", BASE_DIR / "avaliador.db"))
HOST = os.environ.get("AVALIADOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("AVALIADOR_PORT", "8080"))


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
            click_threshold=5,
            average_duration_seconds=45,
        )
        seed_service(
            conn,
            service_id="pagina-inicial-governo",
            name="Página inicial do governo",
            url="https://www.gov.br/",
            important=False,
            click_threshold=8,
            average_duration_seconds=60,
        )
        seed_service(
            conn,
            service_id="servico-publico-generico",
            name="Serviço público genérico",
            url="https://www.gov.br/pt-br/servicos",
            important=False,
            click_threshold=8,
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
        insert or ignore into services
            (id, name, url, important, click_threshold, average_duration_seconds, created_at)
        values (?, ?, ?, ?, ?, ?, ?)
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
        "click_threshold": 8,
        "average_duration_seconds": 60,
    }


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

    return [
        {
            "type": "open_feedback",
            "reason": "duration_above_expected",
            "url": feedback_url(session_id, service_id),
        }
    ]


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
            if route.path == "/health":
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

    def log_message(self, format: str, *args: object) -> None:
        return


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
    .stars label {{ font-size: 28px; cursor: pointer; margin-right: 8px; }}
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
  </style>
</head>
<body>
  <main>
    <h1>Como foi sua experiência?</h1>
    <form id="feedback-form">
      <fieldset class="stars">
        <legend>Avaliação geral</legend>
        <label><input type="radio" name="stars" value="1" required>★</label>
        <label><input type="radio" name="stars" value="2">★</label>
        <label><input type="radio" name="stars" value="3">★</label>
        <label><input type="radio" name="stars" value="4">★</label>
        <label><input type="radio" name="stars" value="5">★</label>
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

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const data = new FormData(form);
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
