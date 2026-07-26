"""Launch BLM V2 + V3 on port 2262."""
import uvicorn
import sys
sys.path.insert(0, "/private/tmp/BLM")

from blm_v2.api.v2_fastapi import create_v2_app

app = create_v2_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=2262, reload=False, log_level="info")
