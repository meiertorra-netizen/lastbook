"""Several local AI models: the app runs the best one found that fits this computer, setup offers the best one that fits,
and a smaller model's thinking never reaches the answer."""
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import llm_local                    # noqa: E402
from engine.setup import api        # noqa: E402

GiB = 2 ** 30


class HideThinking(unittest.TestCase):
    def test_closed_block_goes(self):
        self.assertEqual(llm_local.hide_thinking('<|channel>thought\nhmm<channel|>Boil it for 1 minute.'), 'Boil it for 1 minute.')

    def test_unfinished_block_hides_what_follows(self):
        self.assertEqual(llm_local.hide_thinking('Answer so far <|channel>thought\nstill thinking'), 'Answer so far ')

    def test_stray_close_from_a_smaller_model(self):
        """E4B after a tool response: plain-text thinking, then a close it never opened, then the answer."""
        text = 'The user is asking about boiling. S4 says 1 minute.<channel|>Bring the water to a rolling boil for 1 minute [S4].'
        self.assertEqual(llm_local.hide_thinking(text), 'Bring the water to a rolling boil for 1 minute [S4].')

    def test_plain_answer_is_kept(self):
        self.assertEqual(llm_local.hide_thinking('Use 8 drops per gallon [S1].'), 'Use 8 drops per gallon [S1].')


def fake_models(tmp):
    """Two small model files, best first, as MODELS lists them."""
    out = []
    for mid, data in (('big', b'B' * 4000), ('small', b's' * 1000)):
        p = tmp / f'{mid}.gguf'
        p.write_bytes(data)
        out.append({'id': mid, 'title': mid.title(), 'file': p.name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                    'name': f'{mid.title()} model', 'source': 'test'})
    return out


class Discover(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)
        self.models = fake_models(self.tmp)
        patches = [mock.patch.object(llm_local, 'MODELS', self.models),
                   mock.patch.object(llm_local, 'BY_FILE', {m['file']: m for m in self.models}),
                   mock.patch.object(llm_local, 'places', lambda: [self.tmp]),
                   mock.patch.object(llm_local, 'STATE_FILE', self.tmp / 'state.json'),
                   mock.patch.object(llm_local, 'runner_path', lambda: self.tmp / 'lastbook-llm')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.temp.cleanup()

    def local(self, fits):
        """A Local whose measure says each model fits or not, by id."""
        local = llm_local.Local()

        def measure():
            ok = fits[local.model['id']]
            local.fit = {'fits': ok, 'needs': 1}
            local.state, local.message = ('stopped', '') if ok else ('too_big', f'{local.model["id"]} is too big')
        local._measure = measure
        return local

    def test_best_found_model_that_fits_is_used(self):
        local = self.local({'big': True, 'small': True})
        local.discover()
        self.assertEqual((local.state, local.model['id'], local.path.name), ('stopped', 'big', 'big.gguf'))
        self.assertEqual(local.status()['model'], 'big')

    def test_a_smaller_model_takes_over_when_the_best_does_not_fit(self):
        local = self.local({'big': False, 'small': True})
        local.discover()
        self.assertEqual((local.state, local.model['id']), ('stopped', 'small'))
        self.assertEqual(local.status()['name'], 'Small model')

    def test_none_fits_reports_the_best(self):
        local = self.local({'big': False, 'small': False})
        local.discover()
        self.assertEqual((local.state, local.model['id'], local.message), ('too_big', 'big', 'big is too big'))

    def test_wrong_contents_fall_through_to_the_next(self):
        (self.tmp / 'big.gguf').write_bytes(b'X' * 4000)       # right size, wrong SHA-256
        local = self.local({'big': True, 'small': True})
        local.discover()
        self.assertEqual((local.state, local.model['id']), ('stopped', 'small'))

    def test_checked_once(self):
        local = self.local({'big': True, 'small': True})
        local.discover()
        with mock.patch.object(llm_local.Local, '_check', side_effect=AssertionError('read again')):
            again = self.local({'big': True, 'small': True})
            again.discover()
        self.assertEqual(again.model['id'], 'big')


class SetupChoice(unittest.TestCase):
    MODELS = [{'id': 'big', 'title': 'Big', 'file': 'big.gguf', 'size': 17 * 10 ** 9, 'memory_bytes': 19 * 10 ** 9},
              {'id': 'mid', 'title': 'Mid', 'file': 'mid.gguf', 'size': 7 * 10 ** 9, 'memory_bytes': 9 * 10 ** 9},
              {'id': 'small', 'title': 'Small', 'file': 'small.gguf', 'size': 5 * 10 ** 9, 'memory_bytes': 6 * 10 ** 9}]

    def fit(self, ram_gib, measured=None):
        fake = types.SimpleNamespace(manifest=types.SimpleNamespace(models=lambda: self.MODELS), ai_measure=measured,
                                     measuring=False, llm=lambda: None, _model_path_here=lambda: (None, None))
        hw = {'ram_bytes': ram_gib * GiB, 'gpus': [], 'unified_memory': True}
        return api.Setup.ai_fit(fake, hw)

    def test_the_best_model_that_fits_is_offered(self):
        self.assertEqual(self.fit(32)['model'], 'big')
        self.assertEqual(self.fit(16)['model'], 'mid')
        self.assertEqual(self.fit(10)['model'], 'small')

    def test_options_say_which_fit(self):
        self.assertEqual([(o['model'], o['fits']) for o in self.fit(16)['options']], [('big', False), ('mid', True), ('small', True)])

    def test_nothing_fits_says_the_smallest_need(self):
        f = self.fit(6)
        self.assertFalse(f['fits'])
        self.assertEqual(f['model'], 'small')
        self.assertIn('needs about 8.7 GB', f['why'])      # the smallest model's 6 GB and the 2.5 GiB reserve

    def test_a_measured_file_replaces_its_estimate(self):
        measured = {'fits': True, 'needs_bytes': 8 * 10 ** 9, 'estimate': False, 'why': 'measured', 'model': 'big'}
        self.assertEqual(self.fit(16, measured)['model'], 'big')


if __name__ == '__main__':
    unittest.main()
