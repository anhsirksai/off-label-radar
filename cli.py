"""Build-time runner: `.venv/bin/python cli.py metformin sirolimus` (uses $PROMPTQL_USER_JWT)."""
import asyncio
import os
import sys

import db
import net
import pipeline


async def main():
    db.init()
    args = sys.argv[1:]
    force = "--force" in args
    cond = None
    if "--condition" in args:
        i = args.index("--condition")
        cond, args = args[i + 1], args[:i] + args[i + 2:]
    drugs = [a for a in args if not a.startswith("--")] or ["metformin"]
    st = net.RunState()

    async def ticker():
        while st.running:
            s = st.snapshot()
            print(f"[{s['stage']}] {s['pairs_done']}/{s['pairs_total']}  jev={s['jev_calls']} tok={s['jev_tokens']}  {s['message']}", flush=True)
            await asyncio.sleep(5)

    import condition
    tok = os.environ.get("PROMPTQL_USER_JWT") or os.environ.get("TYPESAFE_API_KEY") or ""
    if not tok:
        sys.exit("Set TYPESAFE_API_KEY in .env (local) or run inside a PromptQL VM (PROMPTQL_USER_JWT).")
    t = asyncio.create_task(condition.run_condition(cond, tok, st, force=force) if cond else pipeline.run(drugs, tok, st, force=force))
    await asyncio.sleep(0.1)
    await asyncio.gather(t, ticker())
    print("\n".join(st.log[-15:]))
    print(st.snapshot())


asyncio.run(main())