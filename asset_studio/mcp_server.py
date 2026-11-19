"""MCP transports use the same HTTP operations as the studio interface."""
from __future__ import annotations
import json
import httpx
from mcp.server.mcpserver import MCPServer


def create_mcp(base_url):
    server = MCPServer('Trellis Local Studio', version='1.0.0', instructions=(
        'Local image-to-3D studio for Trellis Local Studio. Queue jobs rather than launching TRELLIS directly. '
        'Balanced is the verified 512 / unquantized / 4K profile for this 16 GiB Mac. '
        'Detail/master may exceed its memory budget; no silent quality downgrade occurs. '
        'Notes are saved metadata, not text-to-3D conditioning. Completed outputs need visual review '
        'and cleanup before game integration. Only pause/reorder/cancel when requested.'))

    async def request(method, path, payload=None):
        async with httpx.AsyncClient(base_url=base_url, timeout=30, trust_env=False) as client:
            response = await client.request(method, path, json=payload) if payload is not None else await client.request(method, path)
            if response.is_error:
                try: reason = response.json().get('detail', response.text)
                except ValueError: reason = response.text
                raise ValueError(str(reason))
            return response.json()

    @server.tool()
    async def studio_status() -> dict:
        """Get generator readiness, running job, queue state, data location and local URLs."""
        return await request('GET','/api/status')

    @server.tool()
    async def queue_asset(name: str, image_path: str = '', reference_id: str = '', category: str = 'Props',
                          profile: str = 'balanced', seed: int = 42, memory_gib: float = 10., notes: str = '') -> dict:
        """Queue one asset from a local reference image or an existing reference_id. Returns immediately.

        Categories: Vegetation, Buildings, Props, Characters, Creatures, Terrain.
        Profiles: balanced (recommended), legacy, detail, master. Each job keeps its own output.
        Reference must be a single-object image, at least 256 pixels per side. Notes are metadata.
        """
        return await request('POST','/api/jobs', dict(name=name,image_path=image_path or None,reference_id=reference_id or None,
            category=category,profile=profile,seed=seed,memory_gib=memory_gib,notes=notes))

    @server.tool()
    async def list_jobs() -> dict:
        """List queued, active and previous jobs with output paths and failure reasons."""
        return await request('GET','/api/jobs')

    @server.tool()
    async def get_job(job_id: str) -> dict:
        """Get a job's current stage, state, settings and verified output receipt when finished."""
        return await request('GET','/api/jobs/'+job_id)

    @server.tool()
    async def get_job_log(job_id: str) -> dict:
        """Read the bounded tail of a job's real generation log."""
        return await request('GET','/api/jobs/'+job_id+'/log')

    @server.tool()
    async def cancel_job(job_id: str) -> dict:
        """Cancel a queued job or stop a running generator and its child processes. Preserve partial output."""
        return await request('POST','/api/jobs/'+job_id+'/cancel',{})

    @server.tool()
    async def retry_job(job_id: str) -> dict:
        """Create a new queued attempt for a failed/cancelled/interrupted job. Preserve the previous attempt."""
        return await request('POST','/api/jobs/'+job_id+'/retry',{})

    @server.tool()
    async def set_queue_paused(paused: bool) -> dict:
        """Pause/resume new starts. Pausing allows the currently running generation to finish."""
        return await request('PATCH','/api/queue',{'paused':paused})

    @server.tool()
    async def reorder_queue(ordered_ids: list[str]) -> dict:
        """Set the exact pending-job order; include every currently queued job ID once."""
        return await request('POST','/api/queue/order',{'ordered_ids':ordered_ids})

    @server.tool()
    async def list_assets() -> dict:
        """List completed and linked existing assets, with GLB paths and preview URLs."""
        return await request('GET','/api/assets')

    @server.tool()
    async def get_asset(asset_id: str) -> dict:
        """Get a library asset, model location and generation metadata. Does not install it in the game."""
        return await request('GET','/api/assets/'+asset_id)

    @server.tool()
    async def generation_profiles() -> dict:
        """Read exact quality recipes and memory limits. Generation accepts an image, not a text prompt."""
        return await request('GET','/api/profiles')

    @server.resource('trellislocalstudio://studio/status')
    async def status_resource() -> str:
        return json.dumps(await request('GET','/api/status'),indent=2)

    return server
