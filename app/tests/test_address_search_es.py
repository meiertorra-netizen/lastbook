"""Spanish address lookup (indexes named ES-<region>.sqlite): street before number, Catalan and Spanish street types,
accents and articles, and the same rule as the US: an exact house number or nothing."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine.address_search import (es_display, es_norm, es_parse, find_es_addresses, fold, is_address_query,
                                   is_es_address_query)

ROWS = [   # as Overture writes them (scne.es): capitals, type first, articles moved to the end
    ('401', 'CALLE MALLORCA', 'Barcelona', '08013', 41.4032, 2.1748),
    ('92', 'PASEO GRACIA', 'Barcelona', '08008', 41.3953, 2.1616),
    ('92', 'TRAVESSERA GRACIA', 'Barcelona', '08006', 41.3982, 2.1531),
    ('33', 'RAMBLA CATALUNYA', 'Barcelona', '08007', 41.3900, 2.1650),
    ('33', 'AVINGUDA CATALUNYA', 'Godall', '43516', 40.6572, 0.4676),
    ('1', 'PLAÇA CATALUNYA', 'Barcelona', '08002', 41.3870, 2.1700),
    ('2', "CALLE L'HOSPITAL", 'Barcelona', '08001', 41.3800, 2.1700),
    ('7', 'CALLE CUEVAS LAS', 'Tarragona', '43001', 41.1170, 1.2500),
    ('12B', 'CALLE MAJOR', "L'Hospitalet de Llobregat", '08901', 41.3600, 2.1000),
    ('12', 'CALLE MAJOR', 'Santa Bàrbara', '43570', 40.7160, 0.4920),
]


class SpanishAddressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        db = sqlite3.connect(self.geo / 'addresses' / 'ES-CT.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL,cityf TEXT)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?,?)',
                           [(n, s, es_norm(s), c, 'ES-CT', z, la, lo, fold(c)) for n, s, c, z, la, lo in ROWS])
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self.temp.cleanup()

    def names(self, query):
        return [(r['name'], r['detail']) for r in find_es_addresses(query, self.geo)]

    def test_street_before_number_in_either_language(self):
        for query in ('Carrer de Mallorca 401, Barcelona', 'Calle Mallorca 401', 'C/ Mallorca 401 08013', '401 Carrer de Mallorca',
                      'carrer mallorca 401 barcelona'):
            with self.subTest(query=query):
                self.assertEqual(self.names(query), [('Calle Mallorca 401', '08013 Barcelona, ES-CT')])

    def test_street_type_decides_between_same_names(self):
        self.assertEqual(self.names('Passeig de Gràcia 92'), [('Paseo Gracia 92', '08008 Barcelona, ES-CT')])
        self.assertEqual(self.names('Pg. de Gracia 92'), [('Paseo Gracia 92', '08008 Barcelona, ES-CT')])
        self.assertEqual(self.names('Travessera de Gràcia 92'), [('Travessera Gracia 92', '08006 Barcelona, ES-CT')])
        self.assertEqual(self.names('Rambla de Catalunya 33')[0], ('Rambla Catalunya 33', '08007 Barcelona, ES-CT'))
        self.assertEqual(self.names('Plaça de Catalunya 1'), [('Plaça Catalunya 1', '08002 Barcelona, ES-CT')])

    def test_articles_and_apostrophes(self):
        self.assertEqual(self.names("Carrer de l'Hospital 2"), [("Calle l'Hospital 2", '08001 Barcelona, ES-CT')])
        self.assertEqual(self.names('Calle de las Cuevas 7'), [('Calle Las Cuevas 7', '43001 Tarragona, ES-CT')])

    def test_town_and_postcode_narrow_and_never_fall_back(self):
        self.assertEqual(self.names('Carrer Major 12, Santa Barbara'), [('Calle Major 12', '43570 Santa Bàrbara, ES-CT')])
        self.assertEqual(self.names("Carrer Major 12 B L'Hospitalet"), [('Calle Major 12B', "08901 L'Hospitalet de Llobregat, ES-CT")])
        self.assertEqual(self.names('Carrer Major 12, Girona'), [])
        self.assertEqual(self.names('Carrer de Mallorca 401 08001'), [])

    def test_unknown_number_finds_nothing(self):
        self.assertEqual(self.names('Carrer de Mallorca 403'), [])

    def test_only_with_a_spanish_index_installed(self):
        self.assertTrue(is_es_address_query('Carrer de Mallorca 401', self.geo))
        self.assertFalse(is_es_address_query('Barcelona', self.geo))
        empty = Path(self.temp.name) / 'empty'
        (empty / 'addresses').mkdir(parents=True)
        self.assertFalse(is_es_address_query('Carrer de Mallorca 401', empty))
        self.assertFalse(is_address_query('Carrer de Mallorca 401'))     # the US reader is unchanged

    def test_normal_forms(self):
        self.assertEqual(fold("Plaça de l'Àngel"), 'placa de l angel')
        self.assertEqual(fold('Col·legi'), 'collegi')
        self.assertEqual(es_norm('CALLE CUEVAS LAS'), 'cuevas')
        self.assertEqual(es_norm('C/ de les Coves'), 'coves')
        self.assertEqual(es_norm("AVINGUDA D'EMPURIES"), 'empuries')
        self.assertEqual(es_display('CALLE CUEVAS LAS'), 'Calle Las Cuevas')
        self.assertEqual(es_parse('Avinguda Diagonal 640, 08017 Barcelona'), ('640', ['avinguda', 'diagonal'], ['barcelona'], '08017'))


if __name__ == '__main__':
    unittest.main()
