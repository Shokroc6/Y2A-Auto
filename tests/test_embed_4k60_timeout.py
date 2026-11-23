"""Pure timeout regression tests; no application/runtime imports."""
import ast
from pathlib import Path
import unittest


def load_processor():
    source = Path(__file__).resolve().parents[1] / 'modules/task_manager.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TaskProcessor')
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in (
        '_estimate_embed_timeout', '_embed_stage_budget')]
    from modules.video_encoder_params import normalize_cpu_codec
    globals_ = [n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id.startswith('EMBED_TIMEOUT_')
                        for t in n.targets)]
    env = dict(normalize_cpu_codec=normalize_cpu_codec)
    exec(compile(ast.Module(body=[*globals_, cls], type_ignores=[]), str(source), 'exec'), env)
    return env['TaskProcessor']


class Timeout4k60Tests(unittest.TestCase):
    def test_fallback_cpu_gets_same_budget(self):
        processor = load_processor()
        self.assertEqual(processor._embed_stage_budget(
            1171.39, 'x264', 'cpu', 3514, 3840, 2160, '60000/1001'), 14056)

    def test_real_4k2997_outlives_old_timeout_and_observed_run_rate(self):
        p = load_processor()
        # The production run was killed at 1800 s, still making progress at
        # 0.256x. Allow at least 25% headroom over that observed run rate.
        self.assertGreaterEqual(p._estimate_embed_timeout(
            541.694, 'x264', 3840, 2160, '30000/1001'), 541.694 / 0.256 * 1.25)

    def test_cpu_load_tracks_frame_area_and_throughput(self):
        p = load_processor()
        def budget(width, height, fps):
            return p._estimate_embed_timeout(1171.39, 'x264', width, height, fps)

        hd = budget(1920, 1080, 60)
        qhd30 = budget(2560, 1440, 30)
        qhd60 = budget(2560, 1440, '60000/1001')
        uhd15 = budget(3840, 2160, 15)
        uhd30 = budget(3840, 2160, '30000/1001')
        uhd60 = budget(3840, 2160, '60000/1001')
        self.assertLess(hd, qhd30)
        self.assertLess(qhd30, qhd60)
        # 4K15 has the same pixel rate as 1080p60, but larger frames.
        self.assertLess(hd, uhd15)
        self.assertLess(uhd15, uhd30)
        self.assertLess(uhd30, uhd60)
        self.assertLessEqual(abs(budget(3840, 2160, 58.99) - budget(3840, 2160, 59.01)), 10)
        self.assertLessEqual(abs(budget(3840, 2160, 29.99) - budget(3840, 2160, 30.01)), 10)
        self.assertLessEqual(abs(budget(3838, 2160, 30) - budget(3840, 2160, 30)), 10)
        self.assertGreaterEqual(budget(3840, 2160, 120), uhd60)

    def test_unchanged_categories_and_invalid_metadata(self):
        p = load_processor()
        for codec, width, height, fps in [
            ('x264', 1920, 1080, 24), ('x264', 1920, 1080, 30),
            ('x264', 1920, 1080, 60), (None, 3840, 2160, 60),
            ('x264', None, 2160, 60), ('x264', 3840, 2160, None),
            ('x264', 3840, 2160, '0/0'), ('x264', 3840, 2160, 'nan'),
            ('x264', 3840, 2160, 'bad'), ('x264', 3840, 2160, -30),
            ('x264', 3840, 2160, 0), ('x264', -3840, -2160, 60),
            ('x264', 0, 2160, 60), ('x264', float('inf'), 2160, 60),
            ('x264', 3840, float('nan'), 60),
        ]:
            with self.subTest(codec=codec, width=width, fps=fps):
                self.assertEqual(p._estimate_embed_timeout(1171.39, codec, width, height, fps), 3514)
        self.assertEqual(p._estimate_embed_timeout(1171.39, 'x265', 3840, 2160, None), 17570)
        self.assertEqual(p._estimate_embed_timeout(1171.39), 3514)
        self.assertEqual(p._estimate_embed_timeout(100000, 'x265', 1920, 1080, 60), 28800)
        self.assertEqual(p._estimate_embed_timeout(100000, None, 7680, 4320, 120), 10800)

    def test_general_load_applies_to_cpu_fallback_and_codec_switch(self):
        p = load_processor()
        duration = 541.694
        args = (3840, 2160, '30000/1001')
        hardware_budget = p._estimate_embed_timeout(duration, None, *args)
        cpu = p._embed_stage_budget(duration, 'x265', 'cpu', hardware_budget, *args)
        cpu_x264 = p._embed_stage_budget(duration, 'x265', 'cpu_x264', hardware_budget, *args)
        self.assertEqual(hardware_budget, 1800)
        self.assertGreater(cpu_x264, hardware_budget)
        self.assertLessEqual(abs(cpu - 5 * cpu_x264), 4)
        self.assertEqual(cpu, p._estimate_embed_timeout(duration, 'x265', *args))
        self.assertEqual(cpu_x264, p._estimate_embed_timeout(duration, 'x264', *args))

    def test_finite_caps_and_x265_multiplier(self):
        p = load_processor()
        self.assertEqual(p._estimate_embed_timeout(1171.39, 'x265', 3840, 2160, 60), 70283)
        self.assertEqual(p._estimate_embed_timeout(100000, 'x264', 3840, 2160, 60), 43200)
        self.assertEqual(p._estimate_embed_timeout(100000, 'x265', 3840, 2160, 60), 216000)
        self.assertEqual(p._estimate_embed_timeout(None, 'x264', 3840, 2160, 60), 43200)
        self.assertEqual(p._estimate_embed_timeout(10, 'x264', 3840, 2160, 60), 1800)

    def test_stages_and_overall_deadline_include_full_cpu_budget(self):
        p = load_processor()
        args = (3840, 2160, '60000/1001')
        first = p._estimate_embed_timeout(1171.39, None, *args)
        stages = [p._embed_stage_budget(1171.39, 'x265', s, first, *args)
                  for s in ('hw_no_boost', 'cpu', 'cpu_x264')]
        self.assertEqual(stages, [3514, 70283, 14056])
        self.assertEqual(first + sum(stages), 91367)
        # Actual embed call sites must pass the un-defaulted probe metadata;
        # deadline is shared with the same stage budget, not the old first budget.
        text = (Path(__file__).resolve().parents[1] / 'modules/task_manager.py').read_text(encoding='utf-8-sig')
        self.assertEqual(text.count("input_width, input_height, stream_info.get('fps')"), 2)
        self.assertIn('_stage_budget(stage) for stage in retry_stages', text)

    def test_real_4k5994_cpu_gets_twelve_times_duration(self):
        processor = load_processor()
        self.assertEqual(processor._estimate_embed_timeout(
            1171.39, 'x264', 3840, 2160, '60000/1001'), 14056)


if __name__ == '__main__':
    unittest.main()
