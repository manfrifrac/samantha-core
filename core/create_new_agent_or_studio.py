#!/usr/bin/env python3
"""
create_new_agent_or_studio.py
=============================
Generative Agent & Studio Factory for the Samantha Core Ecosystem.

Automated Operations:
1. Registers the agent in PostgreSQL `agents` table with System Prompt, Name, `conv_id`, `work_dir`, and `engine`.
2. Creates the studio directory and universal rule symlinks (`CLAUDE.md -> ../../AGENTS.md`).
3. Generates the initial Obsidian tracking and light memory file.
4. Initializes and launches the dedicated Tmux session and window.
5. Injects the bootstrap identity prompt.
"""

import sys
import os
import time
import uuid
import shlex
import psycopg2
import psycopg2.extras
import subprocess

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import engine_adapter
import agent_db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIOS_DIR = os.path.join(BASE_DIR, "studios")

def create_agent(agent_slug, name, studio_key, system_prompt, custom_work_dir=None, engine="agy", host_machine="VPS", provider=None, model=None):
    agent_slug = agent_slug.lower().strip()
    conv_id = str(uuid.uuid4())

    if not custom_work_dir:
        work_dir = os.path.join(STUDIOS_DIR, studio_key)
    else:
        work_dir = custom_work_dir

    print(f"🚀 [Agent Factory] Initializing creation of agent '{name}' ({agent_slug})...")

    # 1. Create workspace directory & symlink AGENTS.md
    os.makedirs(work_dir, exist_ok=True)
    
    claude_md_path = os.path.join(work_dir, "CLAUDE.md")
    if not os.path.exists(claude_md_path):
        try:
            rel_target = os.path.relpath(os.path.join(BASE_DIR, "AGENTS.md"), work_dir)
            os.symlink(rel_target, claude_md_path)
            print(f"  🔗 Symlink 'CLAUDE.md' linked to '{rel_target}'.")
        except Exception as e:
            print(f"  ⚠️ Symlink warning: {e}")

    # 2. Register in PostgreSQL
    session_name = studio_key
    win_name = f"AGY-{agent_slug.upper()}"

    db = agent_db.get_db()
    if agent_slug in db and not db[agent_slug].get("dismesso", False):
        print(f"[X] Slug '{agent_slug}' already exists and is active. Refusing overwrite.")
        return False

    ag_record = {
        "name": name,
        "conv_id": conv_id,
        "system_prompt": system_prompt,
        "work_dir": work_dir,
        "studio": studio_key,
        "engine": engine,
        "tmux_window": f"{session_name}:{win_name}",
        "host_machine": host_machine,
        "always_on": True,
        "dismesso": False
    }

    if provider:
        ag_record["provider"] = provider
    if model:
        ag_record["model"] = model

    conn = agent_db.get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO agents (agent_id, data)
        VALUES (%s, %s)
        ON CONFLICT (agent_id)
        DO UPDATE SET data = agents.data || EXCLUDED.data;
    """, (agent_slug, psycopg2.extras.Json(ag_record)))
    conn.commit()
    conn.close()
    print(f"  💾 PostgreSQL registration complete (work_dir: {work_dir})")

    # 3. Launch Tmux Window
    launch_cmd = engine_adapter.build_launch_cmd(engine, conv_id, work_dir, system_prompt=system_prompt, host_machine=host_machine, provider=provider, model=model, agent_slug=agent_slug)

    res = subprocess.run(f"tmux has-session -t {session_name} 2>/dev/null", shell=True)
    if res.returncode != 0:
        tmux_cmd = f"tmux new-session -d -s {session_name} -n '{win_name}' {shlex.quote(launch_cmd)}"
    else:
        tmux_cmd = f"tmux new-window -t {session_name} -n '{win_name}' {shlex.quote(launch_cmd)}"

    subprocess.run(tmux_cmd, shell=True)
    print(f"  ⚡ Tmux Window '{session_name}:{win_name}' spawned.")

    target_win = f"{session_name}:{win_name}"
    print(f"  ⏳ Waiting for {engine} CLI interface readiness...")
    ready = False
    for _ in range(40):
        time.sleep(0.5)
        try:
            pane = subprocess.check_output(["tmux", "capture-pane", "-t", target_win, "-p", "-S", "-15"], text=True)
            dialog_key = engine_adapter.detect_startup_dialog(engine, pane)
            if dialog_key:
                engine_adapter.send_via_tmux_buffer(target_win, dialog_key, engine=engine)
                continue
            if engine_adapter.is_pane_ready(engine, pane):
                ready = True
                break
        except Exception:
            pass
    if not ready:
        time.sleep(1.5)

    import prompt_builder
    initial_prompt = (
        f"Hello {name}! You have just been initialized within the Samantha Core Fleet. "
        f"Please briefly introduce yourself and confirm your operational role."
    )
    bootstrap_prompt = prompt_builder.build_bootstrap_prompt(agent_slug, name, initial_prompt, host_machine=host_machine)
    print("  📥 Injecting Bootstrap Prompt via Tmux buffer...")
    engine_adapter.send_via_tmux_buffer(target_win, bootstrap_prompt, engine=engine)

    print(f"🎉 Agent '{name}' successfully created and active!")
    return True

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python3 create_new_agent_or_studio.py <slug> <name> <studio_key> <system_prompt> [work_dir] [engine=agy|claude] [host_machine=VPS]")
        sys.exit(1)

    custom_dir = sys.argv[5] if len(sys.argv) > 5 else None
    engine_arg = sys.argv[6] if len(sys.argv) > 6 else "agy"
    host_machine_arg = sys.argv[7] if len(sys.argv) > 7 else "VPS"

    create_agent(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], custom_dir, engine_arg, host_machine_arg)
