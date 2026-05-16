import asyncio
import sys
from pathlib import Path
from postcheck.core.orchestrator import run_verification
from postcheck.core.types import VerifyOptions

async def main():
    project_root = Path(sys.argv[1]).resolve()
    since = sys.argv[2] if len(sys.argv) > 2 else "HEAD~1"
    opts = VerifyOptions(project_root=project_root, since=since)
    result = await run_verification(opts)
    print(result.model_dump_json(indent=2))

if __name__ == "__main__":
    asyncio.run(main())