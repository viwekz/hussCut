"""The database: accounts, sign-in sessions, notifications and the record of AI use.

PostgreSQL on a server (DATABASE_URL=postgresql+psycopg://...). With no DATABASE_URL it uses a SQLite file,
which is fine for one machine."""
import time

import sqlalchemy as sa

from . import config

_SQLITE = config.DATABASE_URL.startswith("sqlite")
engine = sa.create_engine(config.DATABASE_URL, pool_pre_ping=not _SQLITE, future=True, pool_size=20, max_overflow=40,
                          **({"connect_args": {"timeout": 30}} if _SQLITE else {}))

if _SQLITE:
    @sa.event.listens_for(engine, "connect")
    def _fast_sqlite(conn, _record):
        # WAL: people reading are never held up by someone writing. NORMAL: safe against crashes of the app,
        # without waiting for the disk on every single write.
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()
meta = sa.MetaData()

users = sa.Table(
    "users", meta,
    sa.Column("id", sa.String(40), primary_key=True),
    sa.Column("email", sa.String(254), nullable=False, unique=True),          # always lower case
    sa.Column("name", sa.String(120), nullable=False, server_default=""),
    sa.Column("password_hash", sa.String(300)),                               # empty for Google-only accounts
    sa.Column("google_sub", sa.String(64), unique=True),
    sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("last_seen", sa.Float),
)
sessions = sa.Table(
    "sessions", meta,
    sa.Column("token_hash", sa.String(64), primary_key=True),                 # sha256 of the cookie; the cookie itself is never stored
    sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("ip", sa.String(64)),
    sa.Column("agent", sa.String(300)),
)
usage = sa.Table(                                                             # one row per AI call
    "usage", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("t", sa.Float, nullable=False),
    sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),   # text sent to the AI
    sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),  # text it wrote back
    sa.Column("purpose", sa.String(60), nullable=False),
    sa.Column("creator", sa.String(80)),
    sa.Column("place", sa.String(200)),
    sa.Index("usage_user_t", "user_id", "t"),
)


email_ok = sa.Table(                                                          # addresses proven to belong to the account
    "email_ok", meta,
    sa.Column("user_id", sa.String(40), primary_key=True),                    # "_existing" marks that older accounts were counted
    sa.Column("verified_at", sa.Float, nullable=False),
)
email_tokens = sa.Table(                                                      # one-time links: verify an email, reset a password
    "email_tokens", meta,
    sa.Column("token_hash", sa.String(64), primary_key=True),                 # sha256 of the link token; the token itself is never stored
    sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("purpose", sa.String(10), nullable=False),                      # verify | reset
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("used_at", sa.Float),
)


feedback = sa.Table(                                                          # bug reports and ideas, read by admins
    "feedback", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("t", sa.Float, nullable=False, index=True),
    sa.Column("kind", sa.String(10), nullable=False),                         # bug | idea | other
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("page", sa.String(200), nullable=False, server_default=""),     # where in the app they were
    sa.Column("context", sa.String(1000), nullable=False, server_default=""), # e.g. the error they were looking at
    sa.Column("status", sa.String(10), nullable=False, server_default="new", index=True),  # new | seen | done
    sa.Column("updated_at", sa.Float, nullable=False),
)
notices = sa.Table(                                                           # the bell: things worth telling someone about
    "notices", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("t", sa.Float, nullable=False),
    sa.Column("kind", sa.String(20), nullable=False),                         # live | clips | post | account | feedback
    sa.Column("title", sa.String(160), nullable=False),
    sa.Column("body", sa.String(400), nullable=False, server_default=""),
    sa.Column("link", sa.String(200), nullable=False, server_default=""),
    sa.Column("ref", sa.String(80)),                                          # set when it must only be sent once
    sa.Column("read_at", sa.Float),
    sa.Index("notices_user_t", "user_id", "t"),
)


admin_log = sa.Table(                                                         # what admins did, never edited
    "admin_log", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("t", sa.Float, nullable=False, index=True),
    sa.Column("admin_id", sa.String(40), nullable=False),
    sa.Column("action", sa.String(30), nullable=False),                       # disable | enable | delete | role:admin | role:user
    sa.Column("target_id", sa.String(40)),
    sa.Column("detail", sa.String(300), nullable=False, server_default=""),
)

clip_edits = sa.Table(                                                        # EDL, caption style, audio and overlays
    "clip_edits", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.String(40), nullable=False, index=True),
    sa.Column("job_id", sa.String(60), nullable=False, index=True),
    sa.Column("clip_id", sa.String(60), nullable=False, index=True),
    sa.Column("edl_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("style_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("audio_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("overlays_json", sa.Text, nullable=False, server_default="[]"),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.UniqueConstraint("user_id", "job_id", "clip_id", name="uq_user_job_clip"),
)

sequences = sa.Table(                                                         # multi-clip mixed sequences
    "sequences", meta,
    sa.Column("id", sa.String(60), primary_key=True),
    sa.Column("user_id", sa.String(40), nullable=False, index=True),
    sa.Column("title", sa.String(200), nullable=False, server_default="Untitled Sequence"),
    sa.Column("aspect", sa.String(20), nullable=False, server_default="9:16"),
    sa.Column("items_json", sa.Text, nullable=False, server_default="[]"),
    sa.Column("audio_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)


def init():
    meta.create_all(engine)


def get_clip_edit(user_id: str, job_id: str, clip_id: str) -> dict:
    import json
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(clip_edits).where(
                clip_edits.c.user_id == user_id,
                clip_edits.c.job_id == job_id,
                clip_edits.c.clip_id == clip_id,
            )
        ).mappings().first()
    if not row:
        return {}
    return {
        "job_id": row["job_id"],
        "clip_id": row["clip_id"],
        "edl": json.loads(row["edl_json"] or "{}"),
        "style": json.loads(row["style_json"] or "{}"),
        "audio": json.loads(row["audio_json"] or "{}"),
        "overlays": json.loads(row["overlays_json"] or "[]"),
        "updated_at": row["updated_at"],
    }


def save_clip_edit(user_id: str, job_id: str, clip_id: str, edl: dict = None, style: dict = None,
                   audio: dict = None, overlays: list = None) -> dict:
    import json
    existing = get_clip_edit(user_id, job_id, clip_id)
    cur_edl = edl if edl is not None else existing.get("edl", {})
    cur_style = style if style is not None else existing.get("style", {})
    cur_audio = audio if audio is not None else existing.get("audio", {})
    cur_overlays = overlays if overlays is not None else existing.get("overlays", [])
    now_t = time.time()
    with engine.begin() as conn:
        # Check if row exists
        r = conn.execute(
            sa.select(clip_edits.c.id).where(
                clip_edits.c.user_id == user_id,
                clip_edits.c.job_id == job_id,
                clip_edits.c.clip_id == clip_id,
            )
        ).first()
        if r:
            conn.execute(
                sa.update(clip_edits).where(clip_edits.c.id == r[0]).values(
                    edl_json=json.dumps(cur_edl),
                    style_json=json.dumps(cur_style),
                    audio_json=json.dumps(cur_audio),
                    overlays_json=json.dumps(cur_overlays),
                    updated_at=now_t,
                )
            )
        else:
            conn.execute(
                sa.insert(clip_edits).values(
                    user_id=user_id,
                    job_id=job_id,
                    clip_id=clip_id,
                    edl_json=json.dumps(cur_edl),
                    style_json=json.dumps(cur_style),
                    audio_json=json.dumps(cur_audio),
                    overlays_json=json.dumps(cur_overlays),
                    updated_at=now_t,
                )
            )
    return {"job_id": job_id, "clip_id": clip_id, "edl": cur_edl, "style": cur_style, "audio": cur_audio, "overlays": cur_overlays, "updated_at": now_t}


def list_sequences(user_id: str) -> list:
    import json
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(sequences).where(sequences.c.user_id == user_id).order_by(sequences.c.updated_at.desc())
        ).mappings().all()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": r["title"],
            "aspect": r["aspect"],
            "items": json.loads(r["items_json"] or "[]"),
            "audio": json.loads(r["audio_json"] or "{}"),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return out


def get_sequence(user_id: str, seq_id: str) -> dict:
    import json
    with engine.connect() as conn:
        r = conn.execute(
            sa.select(sequences).where(sequences.c.user_id == user_id, sequences.c.id == seq_id)
        ).mappings().first()
    if not r:
        return {}
    return {
        "id": r["id"],
        "title": r["title"],
        "aspect": r["aspect"],
        "items": json.loads(r["items_json"] or "[]"),
        "audio": json.loads(r["audio_json"] or "{}"),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def save_sequence(user_id: str, seq_id: str, title: str = None, aspect: str = None, items: list = None, audio: dict = None) -> dict:
    import json
    existing = get_sequence(user_id, seq_id)
    cur_title = title if title is not None else (existing.get("title") or "Untitled Sequence")
    cur_aspect = aspect if aspect is not None else (existing.get("aspect") or "9:16")
    cur_items = items if items is not None else existing.get("items", [])
    cur_audio = audio if audio is not None else existing.get("audio", {})
    now_t = time.time()
    with engine.begin() as conn:
        if existing:
            conn.execute(
                sa.update(sequences).where(sequences.c.user_id == user_id, sequences.c.id == seq_id).values(
                    title=cur_title,
                    aspect=cur_aspect,
                    items_json=json.dumps(cur_items),
                    audio_json=json.dumps(cur_audio),
                    updated_at=now_t,
                )
            )
        else:
            conn.execute(
                sa.insert(sequences).values(
                    id=seq_id,
                    user_id=user_id,
                    title=cur_title,
                    aspect=cur_aspect,
                    items_json=json.dumps(cur_items),
                    audio_json=json.dumps(cur_audio),
                    created_at=now_t,
                    updated_at=now_t,
                )
            )
    return {"id": seq_id, "title": cur_title, "aspect": cur_aspect, "items": cur_items, "audio": cur_audio, "updated_at": now_t}


def delete_sequence(user_id: str, seq_id: str) -> bool:
    with engine.begin() as conn:
        res = conn.execute(
            sa.delete(sequences).where(sequences.c.user_id == user_id, sequences.c.id == seq_id)
        )
        return res.rowcount > 0


def now() -> float:
    return time.time()
