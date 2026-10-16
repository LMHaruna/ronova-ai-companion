"""Automatic CUDA inference selection; CPU fallback uses the same runtime."""


def inference_device(torch, requested='auto'):
    if requested not in ('auto', 'cpu', 'cuda'):
        raise ValueError('本地语音设备必须是 auto、cpu 或 cuda')
    result = {'device': 'cpu', 'is_half': False, 'gpu_name': None,
              'torch_version': torch.__version__, 'cuda_version': torch.version.cuda,
              'fallback_reason': None}
    if requested == 'cpu':
        return result
    try:
        if not torch.cuda.is_available():
            result['fallback_reason'] = 'CUDA unavailable'
            return result
        x = torch.ones((8, 8), device='cuda', dtype=torch.float16)
        if (x @ x).sum().item() != 512:
            raise RuntimeError('CUDA kernel check failed')
        torch.cuda.synchronize()
        result.update(device='cuda', is_half=True, gpu_name=torch.cuda.get_device_name(0))
    except Exception as error:
        result['fallback_reason'] = type(error).__name__
    return result
