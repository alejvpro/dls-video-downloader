"""One manually authorized, bounded metadata probe; no asset collection."""
import json
from . import youtube_safety as safety
from . import comment_collection as network
from .caption_collection import listing_command
from .comments import VIDEO_ID


def recover(state, video_id, *, authorized=False, reviewed=False, clock=None):
    clock=clock or safety.utc
    if not VIDEO_ID.fullmatch(video_id):raise ValueError('Expected YouTube video ID')
    from .comment_runner import video_busy
    for relative in ('acquisitions/.acquisition.lock','comments/.collector.lock','captions/.collector.lock','supplemental/.cycle.lock'):
        if video_busy(state.state/relative):raise safety.Blocked('recovery_deferred_active_operation')
    with safety.scope(state.state,'controlled-recovery'):
        with state.recovery(authorized=authorized,reviewed=reviewed,now=clock()) as token:
            execution=network.run_extractor(listing_command(video_id),60,token['fd'])
            diagnostic=execution.stderr.decode(errors='replace')
            if safety.classify(diagnostic):
                if state.status(clock())['stored_status']=='recovery_in_progress':
                    state.observe(diagnostic,component='controlled-recovery',operation='metadata_probe',now=clock())
                raise safety.Blocked('controlled_recovery_blocked')
            if execution.termination or execution.returncode:raise safety.Blocked('controlled_recovery_failed; evidence retained; no automatic retry')
            data=json.loads(next(line for line in reversed(execution.stdout.decode().splitlines()) if line.startswith('{')))
            if not isinstance(data,dict) or data.get('id')!=video_id or data.get('live_status') not in ('was_live','not_live') or data.get('is_live') is True:
                raise safety.Blocked('controlled_recovery_unverified; no automatic retry')
            return state.recovered(token,now=clock())
