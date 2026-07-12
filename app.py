"""
Rumor: Social Deduction Game
Platform: Raspberry Pi 4B / any machine on the local network, Flask + Socket.IO
Goal: Players spread and investigate rumors, gain reputation points.
Two paths to victory: top "Snake" (manipulator) or top "Honest" (truth-seeker).

Changes in this version:
1. Rumor severity now escalates automatically as popularity climbs (Low -> Medium -> High -> Severe).
2. Host controls: the first player to join is the host and gets extra buttons
   (End Round Early, End Game, Reset Lobby) other players don't see.
3. Secret Objectives: each player gets a private goal at game start. These are
   NEVER included in the broadcast game-state socket event (that goes to every
   connected phone identically) - they're only ever served over a private,
   session-authenticated route, and only revealed to everyone once the game
   actually ends.
"""

import os
import time
import random
import sqlite3

from flask import Flask, render_template, request, redirect, url_for, g, session, jsonify
from flask_socketio import SocketIO
from werkzeug.utils import secure_filename

# =====================================================
# CONFIG
# =====================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["UPLOAD_FOLDER"] = "static/uploads"
app.config["DATABASE"] = "game.db"
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

socketio = SocketIO(app, async_mode="threading")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}
ROUND_DURATION = 75   # seconds for the action phase
VOTE_DURATION = 45    # seconds for the voting phase

SEVERITY_TIERS = ["Low", "Medium", "High", "Severe"]

# =====================================================
# DATABASE
# =====================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            descriptor TEXT,
            is_host INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 50,
            influence INTEGER DEFAULT 10,
            acted_this_round INTEGER DEFAULT 0,
            image TEXT,
            snake_score INTEGER DEFAULT 0,
            honesty_score INTEGER DEFAULT 0,
            successful_investigations INTEGER DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS rumors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            subject TEXT,
            truth TEXT,
            base_severity TEXT,
            severity TEXT,
            popularity INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_number INTEGER,
            voter_id INTEGER,
            target_id INTEGER,
            snake_points INTEGER DEFAULT 0,
            honest_points INTEGER DEFAULT 0,
            UNIQUE(round_number, voter_id, target_id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS objectives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER,
            text TEXT,
            obj_type TEXT,
            target_player_id INTEGER,
            threshold_value INTEGER,
            bonus_points INTEGER DEFAULT 10,
            completed INTEGER DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS game_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            phase TEXT DEFAULT 'JOIN',
            round_number INTEGER DEFAULT 1,
            round_end_time INTEGER DEFAULT 0
        )
    """)
    db.execute("INSERT OR IGNORE INTO game_state (id) VALUES (1)")
    db.commit()


# =====================================================
# UTILITY FUNCTIONS
# =====================================================
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_game_state():
    db = get_db()
    return db.execute("SELECT * FROM game_state WHERE id=1").fetchone()


def current_player():
    """Look up the logged-in player from the session, not from the URL."""
    player_id = session.get("player_id")
    if not player_id:
        return None
    db = get_db()
    return db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()


def is_host(player):
    return bool(player) and bool(player["is_host"])


# ---------------- severity escalation ----------------
def escalate_severity(base_severity, popularity):
    """A rumor gets more dangerous the more it spreads, regardless of
    where it started. Popularity 3+ bumps it one tier, 6+ bumps it two,
    10+ maxes it out at Severe."""
    base_rank = SEVERITY_TIERS.index(base_severity) if base_severity in SEVERITY_TIERS else 1
    bonus = 0
    if popularity >= 3:
        bonus += 1
    if popularity >= 6:
        bonus += 1
    if popularity >= 10:
        bonus += 1
    new_rank = min(base_rank + bonus, len(SEVERITY_TIERS) - 1)
    return SEVERITY_TIERS[new_rank]


# ---------------- round / phase management ----------------
def start_new_round(db, state):
    end_time = int(time.time()) + ROUND_DURATION
    db.execute("UPDATE players SET acted_this_round=0")
    db.execute("""
        UPDATE game_state
        SET phase='ACTION', round_number = round_number + 1, round_end_time=?
        WHERE id=1
    """, (end_time,))
    db.commit()


def start_vote_phase(db):
    end_time = int(time.time()) + VOTE_DURATION
    db.execute("UPDATE game_state SET phase='VOTE', round_end_time=? WHERE id=1", (end_time,))
    db.commit()


def tally_votes_and_advance(db, state):
    """Sum every ballot cast this round, apply scores, then start the next round."""
    round_number = state["round_number"]
    rows = db.execute("""
        SELECT target_id, SUM(snake_points) AS snake_total, SUM(honest_points) AS honest_total
        FROM votes WHERE round_number=?
        GROUP BY target_id
    """, (round_number,)).fetchall()

    for row in rows:
        snake_total = row["snake_total"] or 0
        honest_total = row["honest_total"] or 0
        db.execute("""
            UPDATE players
            SET snake_score = snake_score + ?,
                honesty_score = honesty_score + ?,
                reputation = reputation + ? + ?
            WHERE id=?
        """, (snake_total, honest_total, snake_total, honest_total, row["target_id"]))

    db.commit()
    start_new_round(db, state)


def initialize_rumors(db):
    db.execute("DELETE FROM rumors")
    sample_rumors = [
        ("Maya's been quietly asking about maternity leave.", "Maya", "Semi-True", "Medium"),
        ("Sade's 'flu' is a lot more convenient than it looks.", "Sade", "True", "High"),
        ("Chris signed off on invoices nobody can find receipts for.", "Chris", "Semi-True", "High"),
        ("Jordan's been seen leaving Sade's place at 6am. Every day.", "Jordan", "True", "Low"),
        ("Zara's app has been quietly logging everyone's location.", "Zara", "Semi-True", "Medium"),
    ]
    for text, subject, truth, severity in sample_rumors:
        db.execute(
            "INSERT INTO rumors (text, subject, truth, base_severity, severity) VALUES (?, ?, ?, ?, ?)",
            (text, subject, truth, severity, severity),
        )
    db.commit()


# ---------------- secret objectives ----------------
OBJECTIVE_TYPES = ["influence_threshold", "reputation_threshold", "expose_a_lie", "outsnake", "outhonest", "undermine_target"]


def generate_objective(player, others):
    """Build one private objective for a player. Falls back to a
    non-targeted objective if there's no one else to target yet."""
    available_types = list(OBJECTIVE_TYPES)
    if not others:
        available_types = ["influence_threshold", "reputation_threshold", "expose_a_lie"]

    obj_type = random.choice(available_types)

    if obj_type == "influence_threshold":
        threshold = player["influence"] + random.randint(12, 20)
        return {"text": f"Reach {threshold} Influence.", "type": obj_type, "target_id": None, "threshold": threshold}

    if obj_type == "reputation_threshold":
        threshold = player["reputation"] + random.randint(15, 25)
        return {"text": f"Reach {threshold} Reputation.", "type": obj_type, "target_id": None, "threshold": threshold}

    if obj_type == "expose_a_lie":
        return {"text": "Successfully expose one false rumor before the game ends.", "type": obj_type, "target_id": None, "threshold": None}

    target = random.choice(others)
    if obj_type == "outsnake":
        return {"text": f"End the game with a higher Snake Score than {target['name']}.", "type": obj_type, "target_id": target["id"], "threshold": None}

    if obj_type == "outhonest":
        return {"text": f"End the game with a higher Honesty Score than {target['name']}.", "type": obj_type, "target_id": target["id"], "threshold": None}

    if obj_type == "undermine_target":
        return {"text": f"Get {target['name']}'s Reputation to drop below {target['reputation']}.", "type": obj_type, "target_id": target["id"], "threshold": target["reputation"]}


def assign_objectives(db):
    players = [dict(p) for p in db.execute("SELECT * FROM players").fetchall()]
    db.execute("DELETE FROM objectives")
    for player in players:
        others = [p for p in players if p["id"] != player["id"]]
        obj = generate_objective(player, others)
        db.execute(
            "INSERT INTO objectives (player_id, text, obj_type, target_player_id, threshold_value) VALUES (?, ?, ?, ?, ?)",
            (player["id"], obj["text"], obj["type"], obj["target_id"], obj["threshold"]),
        )
    db.commit()


def evaluate_objectives(db):
    """Called once, when the host ends the game. Checks every objective
    against final state, pays out bonus points, and marks them complete
    so the reveal screen can show who nailed theirs."""
    objectives = db.execute("SELECT * FROM objectives").fetchall()
    players_by_id = {p["id"]: dict(p) for p in db.execute("SELECT * FROM players").fetchall()}

    for obj in objectives:
        player = players_by_id.get(obj["player_id"])
        if not player:
            continue
        completed = False

        if obj["obj_type"] == "influence_threshold":
            completed = player["influence"] >= obj["threshold_value"]
        elif obj["obj_type"] == "reputation_threshold":
            completed = player["reputation"] >= obj["threshold_value"]
        elif obj["obj_type"] == "expose_a_lie":
            completed = player["successful_investigations"] >= 1
        elif obj["obj_type"] == "outsnake":
            target = players_by_id.get(obj["target_player_id"])
            completed = target is not None and player["snake_score"] > target["snake_score"]
        elif obj["obj_type"] == "outhonest":
            target = players_by_id.get(obj["target_player_id"])
            completed = target is not None and player["honesty_score"] > target["honesty_score"]
        elif obj["obj_type"] == "undermine_target":
            target = players_by_id.get(obj["target_player_id"])
            completed = target is not None and target["reputation"] < obj["threshold_value"]

        if completed:
            db.execute("UPDATE players SET reputation = reputation + ? WHERE id=?", (obj["bonus_points"], obj["player_id"]))
            db.execute("UPDATE objectives SET completed=1 WHERE id=?", (obj["id"],))

    db.commit()


# ---------------- state serialization ----------------
def serialize_state():
    """Built for the broadcast socket event. Deliberately contains NOTHING
    private - no one's secret objective shows up here, only the public board."""
    db = get_db()
    state = get_game_state()
    now = int(time.time())
    remaining = max(0, state["round_end_time"] - now)

    players = [dict(p) for p in db.execute(
        "SELECT id, name, descriptor, is_host, reputation, influence, image, snake_score, honesty_score, acted_this_round FROM players"
    ).fetchall()]
    rumors = [dict(r) for r in db.execute(
        "SELECT id, text, subject, severity, popularity, active FROM rumors WHERE active=1"
    ).fetchall()]

    payload = {
        "phase": state["phase"],
        "round_number": state["round_number"],
        "remaining_time": remaining,
        "players": players,
        "rumors": rumors,
        "objectives_reveal": [],
    }

    if state["phase"] == "GAME_OVER":
        reveal_rows = db.execute("""
            SELECT objectives.text, objectives.completed, players.name AS player_name
            FROM objectives JOIN players ON players.id = objectives.player_id
        """).fetchall()
        payload["objectives_reveal"] = [dict(r) for r in reveal_rows]

    return payload


# =====================================================
# BACKGROUND LOOP — advances phases automatically and
# pushes live state to every connected phone once a second.
# =====================================================
def game_clock():
    while True:
        socketio.sleep(1)
        with app.app_context():
            db = get_db()
            state = get_game_state()
            now = int(time.time())

            if state["phase"] == "ACTION" and now >= state["round_end_time"]:
                start_vote_phase(db)
            elif state["phase"] == "VOTE" and now >= state["round_end_time"]:
                tally_votes_and_advance(db, get_game_state())

            socketio.emit("game-state", serialize_state())


# =====================================================
# ROUTES
# =====================================================
@app.route("/")
def index():
    player = current_player()
    if not player:
        return redirect(url_for("join"))
    return render_template("index.html", me=player, state=serialize_state())


@app.route("/join", methods=["GET", "POST"])
def join():
    state = get_game_state()
    if request.method == "POST":
        if state["phase"] != "JOIN":
            return render_template("join.html", error="The game is already in progress. Wait for the host to reset the lobby.")

        name = request.form["name"].strip()
        descriptor = request.form.get("descriptor", "").strip()

        image_path = None
        file = request.files.get("photo")
        if file and file.filename != "" and allowed_file(file.filename):
            filename = secure_filename(f"{int(time.time())}_{file.filename}")
            path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            file.save(path)
            image_path = "/" + path

        db = get_db()
        player_count = db.execute("SELECT COUNT(*) AS c FROM players").fetchone()["c"]
        cur = db.execute(
            "INSERT INTO players (name, descriptor, image, is_host) VALUES (?, ?, ?, ?)",
            (name, descriptor, image_path, 1 if player_count == 0 else 0),
        )
        db.commit()
        session["player_id"] = cur.lastrowid
        return redirect(url_for("index"))
    return render_template("join.html", error=None)


@app.route("/my-objective")
def my_objective():
    """Private, session-authenticated. This is the ONLY place a player's
    secret objective is ever served before the game ends."""
    player = current_player()
    if not player:
        return jsonify({"error": "not joined"}), 403
    db = get_db()
    obj = db.execute("SELECT text FROM objectives WHERE player_id=?", (player["id"],)).fetchone()
    if not obj:
        return jsonify({"text": None})
    return jsonify({"text": obj["text"]})


# ---------------- host-only routes ----------------
@app.route("/host/start_game")
def host_start_game():
    player = current_player()
    if not is_host(player):
        return "Only the host can start the game.", 403
    db = get_db()
    initialize_rumors(db)
    assign_objectives(db)
    start_new_round(db, get_game_state())
    return redirect(url_for("index"))


@app.route("/host/end_round")
def host_end_round():
    player = current_player()
    if not is_host(player):
        return "Only the host can do that.", 403
    db = get_db()
    state = get_game_state()
    if state["phase"] == "ACTION":
        start_vote_phase(db)
    elif state["phase"] == "VOTE":
        tally_votes_and_advance(db, state)
    return redirect(url_for("index"))


@app.route("/host/end_game")
def host_end_game():
    player = current_player()
    if not is_host(player):
        return "Only the host can do that.", 403
    db = get_db()
    evaluate_objectives(db)
    db.execute("UPDATE game_state SET phase='GAME_OVER' WHERE id=1")
    db.commit()
    return redirect(url_for("index"))


@app.route("/host/reset")
def host_reset():
    player = current_player()
    if not is_host(player):
        return "Only the host can do that.", 403
    db = get_db()
    db.execute("DELETE FROM players")
    db.execute("DELETE FROM rumors")
    db.execute("DELETE FROM votes")
    db.execute("DELETE FROM objectives")
    db.execute("UPDATE game_state SET phase='JOIN', round_number=1, round_end_time=0 WHERE id=1")
    db.commit()
    session.clear()
    return redirect(url_for("join"))


# ---------------- player action routes ----------------
@app.route("/spread", methods=["POST"])
def spread():
    player = current_player()
    state = get_game_state()
    if not player or state["phase"] != "ACTION":
        return redirect(url_for("index"))
    if player["acted_this_round"]:
        return redirect(url_for("index"))

    rumor_id = request.form.get("rumor_id")
    db = get_db()
    rumor = db.execute("SELECT * FROM rumors WHERE id=?", (rumor_id,)).fetchone()
    if rumor:
        new_popularity = rumor["popularity"] + 1
        new_severity = escalate_severity(rumor["base_severity"], new_popularity)
        db.execute(
            "UPDATE rumors SET popularity=?, severity=? WHERE id=?",
            (new_popularity, new_severity, rumor_id),
        )
    db.execute("UPDATE players SET influence = influence + 2, acted_this_round=1 WHERE id=?", (player["id"],))
    db.commit()
    return redirect(url_for("index"))


@app.route("/investigate", methods=["POST"])
def investigate():
    player = current_player()
    state = get_game_state()
    if not player or state["phase"] != "ACTION":
        return redirect(url_for("index"))
    if player["acted_this_round"]:
        return redirect(url_for("index"))

    rumor_id = request.form.get("rumor_id")
    db = get_db()
    rumor = db.execute("SELECT * FROM rumors WHERE id=?", (rumor_id,)).fetchone()

    if rumor:
        chance = player["influence"] + random.randint(0, 40)
        if rumor["truth"] == "False" and chance > 60:
            db.execute("UPDATE rumors SET active=0 WHERE id=?", (rumor_id,))
            db.execute(
                "UPDATE players SET influence = influence + 3, reputation = reputation + 2, successful_investigations = successful_investigations + 1 WHERE id=?",
                (player["id"],),
            )

    db.execute("UPDATE players SET acted_this_round=1 WHERE id=?", (player["id"],))
    db.commit()
    return redirect(url_for("index"))


@app.route("/vote", methods=["GET", "POST"])
def vote():
    player = current_player()
    state = get_game_state()
    if not player:
        return redirect(url_for("join"))
    if state["phase"] != "VOTE":
        return redirect(url_for("index"))

    db = get_db()
    others = db.execute("SELECT * FROM players WHERE id != ?", (player["id"],)).fetchall()

    if request.method == "POST":
        for other in others:
            snake_points = int(request.form.get(f"snake_{other['id']}", 0) or 0)
            honest_points = int(request.form.get(f"honest_{other['id']}", 0) or 0)
            db.execute("""
                INSERT INTO votes (round_number, voter_id, target_id, snake_points, honest_points)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(round_number, voter_id, target_id)
                DO UPDATE SET snake_points=excluded.snake_points, honest_points=excluded.honest_points
            """, (state["round_number"], player["id"], other["id"], snake_points, honest_points))
        db.commit()
        return redirect(url_for("index"))

    return render_template("vote.html", me=player, others=others, state=serialize_state())


# =====================================================
# SOCKET EVENTS
# =====================================================
@socketio.on("connect")
def on_connect():
    socketio.emit("game-state", serialize_state())


# =====================================================
# MAIN
# =====================================================
if __name__ == "__main__":
    with app.app_context():
        init_db()
    socketio.start_background_task(game_clock)
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
