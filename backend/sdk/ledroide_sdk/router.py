from fastapi import APIRouter

# Module authors use @router.get("/...") etc.
# The registry mounts this under /modules/{id}/api at activation time.
router = APIRouter()
