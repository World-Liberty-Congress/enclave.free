#!/usr/bin/env python3
"""Local-only live-model regression for Admin Database discovery and usage.

Requires the disposable `liberator-admin-db-local` Compose project. --seed
refuses a nonempty roster. Credentials stay in memory; reports contain only
synthetic fixture answers and redacted tool traces. No production data is used.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request
import uuid


NAMES = ["Avery", "Blair", "Casey", "Drew", "Emery", "Flynn"]
EXPECTED = [("Avery", 17, 2), ("Drew", 9, 1), ("Blair", 6, 1), ("Flynn", 4, 1), ("Casey", 0, 0)]
QUESTION = "Who are the top 5 users in the db by usage?"


def container_python(container, source):
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-"],
        input=source, text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:28000")
    parser.add_argument("--container", default="liberator-admin-db-core")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--case", choices=["ranking", "inventory", "structure", "no-tools", "user", "partial-identities"], default="ranking")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if urllib.parse.urlparse(args.api_base).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("This fixture harness only supports localhost")
    label = subprocess.check_output([
        "docker", "inspect", "--format",
        '{{index .Config.Labels "com.docker.compose.project"}}', args.container,
    ], text=True).strip()
    if label != "liberator-admin-db-local":
        parser.error("Refusing to use a container outside the disposable fixture project")

    if args.seed:
        container_python(args.container, '''
import json, database
from coincurve import PrivateKey
assert not database.list_admins(), "Fixture requires an empty admin table"
assert database.get_connection().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0, "Fixture requires an empty roster"
database.add_admin(PrivateKey().public_key.format(compressed=True)[1:].hex())
names = ["Avery", "Blair", "Casey", "Drew", "Emery", "Flynn"]
for name in names:
    database.create_user(name=name, email=name.lower() + "@example.test")
with database.get_cursor() as cursor:
    cursor.execute("UPDATE users SET approved=1")
    for i, (user_id, source, turns) in enumerate([(1,"user",10),(1,"user",7),(2,"user",6),(3,"admin_test",1000),(4,"user",9),(6,"user",4)]):
        cursor.execute("INSERT INTO session_logs(log_id,source,subject_user_id,turn_count,updated_at) VALUES (?,?,?,?,?)", (f"admin-db-fixture-{i}",source,user_id,turns,f"2026-10-{10+i:02d} 12:00:00"))
print(json.dumps({"seeded": True}))
''')

    credentials = container_python(args.container, '''
import json, auth, database
assert database.get_connection().execute("SELECT COUNT(*) FROM session_logs WHERE log_id LIKE 'admin-db-fixture-%'").fetchone()[0] == 6, "Missing synthetic fixture"
admin = database.list_admins()[0]
print(json.dumps({
    "admin": auth.create_admin_session_token(admin["id"], admin["pubkey"], int(admin.get("session_nonce", 0) or 0)),
    "user": auth.create_session_token(1, "avery@example.test"),
    "expected_inventory": {
        "total_users": database.get_connection().execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "active_users": database.get_connection().execute("SELECT COUNT(DISTINCT u.id) FROM users u JOIN session_logs l ON l.subject_user_id=u.id WHERE l.source='user' AND l.turn_count > 0").fetchone()[0]
    }
}))
''')
    message = QUESTION
    if args.case == "inventory":
        message = "How many users are in the database, and how many have any recorded usage?"
    if args.case == "structure":
        message = "Which columns in session_logs link a logged conversation to a user and describe its usage? Check the live table schema."
    context = {"source": "admin-signer-user-roster", "users": [
        {"id": i + 1, "name": name} for i, name in enumerate(NAMES)
    ]}
    if args.case == "partial-identities":
        context["users"] = context["users"][:2]
        context["truncated"] = True
    payload = {
        "message": message,
        "session_id": str(uuid.uuid4()),
        "tools": [] if args.case == "no-tools" else ["db-query"],
        "client_decrypted_context": context,
    }
    request = urllib.request.Request(
        args.api_base.rstrip("/") + "/llm/chat/stream",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + credentials["user" if args.case == "user" else "admin"],
                 "Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    answer = ""
    done = None
    events = []
    with urllib.request.urlopen(request, timeout=180) as response:
        event = ""
        for raw in response:
            line = raw.decode().strip()
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
                if event == "answer_delta":
                    answer += data.get("delta", "")
                if event == "done":
                    done = data
                if event == "error":
                    raise RuntimeError(data.get("detail", "Stream failed"))
                if event == "trace_delta":
                    events.append(data.get("trace_delta", {}))
    assert done is not None, "Stream did not complete"
    assert answer.strip(), "Stream returned no answer"
    if args.case == "user":
        # Remove only this synthetic turn's review log so repeated rankings
        # still exercise the same fixture (the Sage chat remains inspectable).
        container_python(args.container, f'''
import database, json
with database.get_cursor() as cursor:
    cursor.execute("DELETE FROM session_logs WHERE sage_session_id = ?", ({payload["session_id"]!r},))
print(json.dumps({{"cleaned": True}}))
''')
    tools = done.get("tools_used", [])
    report = {"case": args.case, "question": message, "answer": answer,
              "model": done.get("model"), "tools": tools, "expected_ranking": EXPECTED,
              "expected_inventory": credentials["expected_inventory"],
              "trace_events": events}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    if args.case in {"user", "no-tools"}:
        assert not tools, "Database tools must not be exposed"
        hidden_names = NAMES[1:] if args.case == "user" else NAMES
        assert not any(name in answer for name in hidden_names), "Decrypted roster leaked into an unauthorized turn"
    else:
        assert any(t["tool_name"] == "Database Schema" for t in tools), "Schema was not discovered"
        if args.case == "structure":
            assert "subject_user_id" in answer and "turn_count" in answer, "Missing user-link or usage column"
        else:
            assert any(t["tool_name"] == "Database Query" and not t.get("guarded") for t in tools), "No successful database query"
        assert not any(t.get("guarded") for t in tools), "The model guessed a rejected query"
        if args.case == "ranking":
            # Check each identity's own segment (the answer may be a table,
            # list, or a single sentence), not numbers anywhere in the answer.
            positions = []
            for name, turns, sessions in EXPECTED:
                start = answer.index(name)
                end = min([answer.find(other, start + len(name)) for other in NAMES
                           if answer.find(other, start + len(name)) >= 0] or [len(answer)])
                segment = answer[start:end]
                assert re.search(rf"\b{turns}\b", segment), f"Wrong recorded turns for {name}: {segment}"
                assert re.search(rf"\b{sessions}\b", segment), f"Wrong conversation count for {name}: {segment}"
                positions.append(start)
            assert positions == sorted(positions), "Ranking order is incorrect"
            assert "recorded" in answer.lower() and "turn" in answer.lower(), "Metric not labeled"
        if args.case == "partial-identities":
            assert not any(name in answer for name in NAMES[2:]), "Unavailable identities were invented"
        if args.case == "inventory":
            expected = credentials["expected_inventory"]
            # Compare the stated population count, not incidental numbers in
            # a later note about the decrypted roster subset.
            plain = answer.replace("*", "")
            total = re.search(r"\b(\d+)\s+(?:total\s+)?users\b", plain, re.I)
            assert total, "Could not locate the reported user count; inspect the answer"
            assert int(total[1]) == expected["total_users"], "Wrong roster size"
            active = re.search(r"\b(\d+)\b", plain[total.end():])
            assert active and int(active[1]) == expected["active_users"], "Wrong active-user count"
    print(json.dumps({"case": args.case, "status": "passed", "model": done.get("model"),
                      "tool_calls": len(tools), "report": str(args.output)}))


if __name__ == "__main__":
    main()
