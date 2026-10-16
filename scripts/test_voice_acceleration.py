"""CPU/GPU inference fallback checks; no real CUDA or audio device."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from voice_acceleration import inference_device


class AccelerationTests(unittest.TestCase):
    def fake_torch(self, available=True):
        torch=MagicMock()
        torch.__version__='2.7.1+cu128'
        torch.version=SimpleNamespace(cuda='12.8')
        torch.cuda.is_available.return_value=available
        torch.cuda.get_device_name.return_value='Fixture GPU'
        torch.ones.return_value.__matmul__.return_value.sum.return_value.item.return_value=512
        return torch

    def test_cpu_and_absent_gpu_never_start_cuda_kernels(self):
        for available,requested in ((True,'cpu'),(False,'auto')):
            torch=self.fake_torch(available)
            result=inference_device(torch,requested)
            self.assertEqual(result['device'],'cpu'); self.assertFalse(result['is_half'])
            torch.ones.assert_not_called()

    def test_supported_gpu_runs_kernel_before_enabling_half_precision(self):
        torch=self.fake_torch()
        result=inference_device(torch)
        self.assertEqual(result['device'],'cuda'); self.assertTrue(result['is_half'])
        self.assertEqual(result['gpu_name'],'Fixture GPU')
        torch.cuda.synchronize.assert_called_once()

    def test_cuda_kernel_or_driver_failure_falls_back_to_full_precision_cpu(self):
        torch=self.fake_torch(); torch.ones.side_effect=RuntimeError('unsupported kernel')
        result=inference_device(torch)
        self.assertEqual(result['device'],'cpu'); self.assertFalse(result['is_half'])
        self.assertEqual(result['fallback_reason'],'RuntimeError')


if __name__=='__main__': unittest.main()
