"""One Serverless invocation drains a small batch, then releases its wake slot.
RunPod input contains only an expiring coordination token, never audio URLs/keys.
"""
import importlib.util
import pathlib
import os

spec = importlib.util.spec_from_file_location('separator', pathlib.Path(__file__).with_name('separate-tracks.py'))
separator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(separator)

def handler(request):
    wake = request.get('input', {}).get('wake', '')
    if not isinstance(wake, str) or len(wake) != 36:
        raise ValueError('Invalid wake token')
    if not separator.BASE.startswith('https://') or not separator.KEY:
        raise RuntimeError('Missing platform configuration')
    completed = 0
    try:
        # Bound each invocation. The platform dispatches any remaining queue on release.
        for _ in range(3):
            job = separator.call('claim', data={}, wake=wake)['job']
            if not job:
                break
            job['wake'] = wake
            separator.process(job)
            completed += 1
        return {'processed': completed}
    finally:
        # Never wait idle or poll forever. Webhook is the fallback for crashes/timeouts.
        separator.call('release', data={}, wake=wake)

if __name__ == '__main__':
    import torch
    import runpod
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA GPU unavailable')
    os.environ['SEPARATION_DEVICE'] = 'cuda'
    runpod.serverless.start({'handler': handler})
