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
    env = dict(normalize_cpu_codec=lambda c: 'x265' if c == 'x265' else 'x264',
               EMBED_TIMEOUT_X265_FACTOR=5, EMBED_TIMEOUT_MAX_SECONDS_X265=54000,
               EMBED_TIMEOUT_MAX_SECONDS=10800, EMBED_TIMEOUT_MIN_SECONDS=1800)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(source), 'exec'), env)
    return env['TaskProcessor']


class Timeout4k60Tests(unittest.TestCase):
    def test_fallback_cpu_gets_same_budget(self):
        processor = load_processor()
        self.assertEqual(processor._embed_stage_budget(
            1171.39, 'x264', 'cpu', 3514, 3840, 2160, '60000/1001'), 14056)

    def test_unchanged_categories_and_invalid_metadata(self):
        p = load_processor()
        for codec, width, height, fps in [
            ('x264', 1920, 1080, 60), ('x264', 3840, 2160, 30),
            (None, 3840, 2160, 60), ('x264', None, 2160, 60),
            ('x264', 3840, 2160, None), ('x264', 3840, 2160, '0/0'),
            ('x264', 3840, 2160, 'nan'), ('x264', 3840, 2160, 'bad'),
            ('x264', -3840, -2160, 60), ('x264', 3840, 2160, 120),
        ]:
            with self.subTest(codec=codec, width=width, fps=fps):
                self.assertEqual(p._estimate_embed_timeout(1171.39, codec, width, height, fps), 3514)
        self.assertEqual(p._estimate_embed_timeout(1171.39, 'x265', 3840, 2160, 30), 17570)
        self.assertEqual(p._estimate_embed_timeout(1171.39), 3514)

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
