"""Read-only address lookup using the state indexes installed with offline maps."""
import math
import re
import sqlite3
import unicodedata
from pathlib import Path

from engine.paths import ro_uri

# Keep normalization compatible with tools/build_addresses.py.
WORDS = {
    'boulevard': 'blvd', 'avenue': 'ave', 'av': 'ave', 'street': 'st',
    'drive': 'dr', 'road': 'rd', 'place': 'pl', 'court': 'ct', 'lane': 'ln',
    'terrace': 'ter', 'circle': 'cir', 'highway': 'hwy', 'parkway': 'pkwy',
    'square': 'sq', 'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
    'mount': 'mt', 'saint': 'st',
}
STATE_NAMES = (
    'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|'
    'District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|'
    'Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|'
    'Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|'
    'New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|'
    'Pennsylvania|Puerto Rico|Rhode Island|South Carolina|South Dakota|'
    'Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming'
).split('|')
STATE_CODES = 'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY'.split()
STATES = dict(zip((name.lower() for name in STATE_NAMES), STATE_CODES))
ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
ORDINALS = 'zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth'.split()
TO_DIGITS = {word: str(number) for number, word in enumerate(ONES)}
for number, word in enumerate(ORDINALS):
    suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    TO_DIGITS[word] = f'{number}{suffix}'
TO_WORDS = {value: key for key, value in TO_DIGITS.items()}
# How a street can end. Three of these are also state codes: Ct (Court, Connecticut), Mt (Mount, Montana), NE (northeast, Nebraska).
STREET_ENDINGS = set(WORDS.values()) | {'ne', 'nw', 'se', 'sw'}


def normalize(text):
    return ' '.join(WORDS.get(word, word) for word in re.sub(r'[^a-z0-9 ]+', ' ', text.lower()).split())


def is_address_query(query):
    """A standalone ZIP remains a place query. A number followed by words is an address."""
    return bool(re.match(r'^\s*\d[\w-]*\s+\S', query))


def street_forms(street):
    words = street.split()
    forms = [street, ' '.join(TO_DIGITS.get(word, word) for word in words),
             ' '.join(TO_WORDS.get(word, word) for word in words)]
    forms += [re.sub(r'\b(\d+)(st|nd|rd|th)\b', r'\1', form) for form in forms[:]]
    if words[0] not in ('n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'):
        forms += [f'{direction} {form}' for form in forms[:] for direction in ('n', 's', 'e', 'w')]
    return list(dict.fromkeys(forms))


def _parts(query, states=True):
    number, remainder = query.strip().split(None, 1)
    number = number.upper()
    remainder = re.sub(r'[^a-z0-9 ]+', ' ', remainder.lower()).split()
    # A ZIP+4 ("62704-1234", or "62704 1234" once the hyphen is gone) is the same place as its five-digit ZIP, which the
    # index keeps: drop the four extra digits so the ZIP and the state before it are still read.
    if len(remainder) > 1 and re.fullmatch(r'\d{4}', remainder[-1]) and re.fullmatch(r'\d{5}', remainder[-2]):
        remainder.pop()
    postcode = ''
    if remainder and re.fullmatch(r'\d{5}', remainder[-1]):
        postcode = remainder.pop()
    state = ''
    for count in (3, 2, 1):
        if not states or len(remainder) <= count:
            continue
        name = ' '.join(remainder[-count:])
        code = STATES.get(name) or (name.upper() if count == 1 and name.upper() in STATE_CODES else '')
        if code:
            state = code
            remainder = remainder[:-count]
            break
    return number, normalize(' '.join(remainder)).split(), state, postcode


def _readings(query):
    """The ways to read a query, likeliest first, as (number, words, state, postcode, town).
    Words at the end that name a state can also be the street's own ending ('12 Oak Ct', '500 4th St NE') or a town
    ('Washington'), so the query is read both ways. The second reading stays narrow, so that a state that is not installed
    still finds nothing: a two-letter code is tried only as part of the street (town ''), and a state's name only as a town
    of exactly that name (town 'whole'). '100 Main St IN' does not find Inglewood, nor 'Maple Ave Kansas' Kansas City."""
    number, words, state, postcode = _parts(query)
    if not state:
        return [(number, words, state, postcode, 'starts')]
    typed = _parts(query, states=False)
    code = len(typed[1]) == len(words) + 1 and typed[1][-1].upper() in STATE_CODES
    by_state, as_typed = (number, words, state, postcode, 'starts'), (*typed, '' if code else 'whole')
    return [as_typed, by_state] if code and typed[1][-1] in STREET_ENDINGS else [by_state, as_typed]


def find_addresses(query, geo_directory, limit=8, near=None):
    """Return exact house-number matches from installed indexes, never a guessed house."""
    if not is_address_query(query):
        return []
    for reading in _readings(query):
        found = _lookup(*reading, geo_directory, limit, near)
        if found:
            return found
    return []


def _lookup(number, words, state, postcode, town, geo_directory, limit, near):
    if not words:
        return []
    geo_directory = Path(geo_directory)
    paths = sorted((geo_directory / 'addresses').glob('*.sqlite'))
    if state:
        paths = [path for path in paths if path.stem.upper() == state]
    legacy = geo_directory / 'addresses.sqlite'
    if not paths and legacy.is_file():
        paths = [legacy]
    connections = []
    try:
        for path in paths:
            try:
                connections.append(sqlite3.connect(ro_uri(path), uri=True))
            except sqlite3.Error:
                continue
        for count in range(len(words), 0 if town else len(words) - 1, -1):
            street = ' '.join(words[:count])
            if all(word in set(WORDS.values()) for word in words[:count]):
                continue
            city = ' '.join(words[count:])
            rows = []
            for form in street_forms(street):
                conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
                parameters = [number, number.lower(), form, form + '\uffff']
                if state:
                    conditions.append('state = ?')
                    parameters.append(state)
                if postcode:
                    conditions.append('zip = ?')
                    parameters.append(postcode)
                if city:
                    conditions.append('lower(city) = ?' if town == 'whole' else 'lower(city) LIKE ?')
                    parameters.append(city if town == 'whole' else city + '%')
                sql = ('SELECT num,street,city,state,zip,lat,lon,norm FROM addr WHERE '
                       + ' AND '.join(conditions) + ' ORDER BY length(norm),norm LIMIT ?')
                for connection in connections:
                    try:
                        rows.extend(connection.execute(sql, [*parameters, limit * 3]).fetchall())
                    except sqlite3.Error:
                        continue
            if rows:
                def rank(row):
                    distance = 0
                    if near:
                        distance = (row[5] - near[0]) ** 2 + ((row[6] - near[1]) * math.cos(math.radians(near[0]))) ** 2
                    return len(row[7]), distance, row[3], row[2], row[1]
                results, seen = [], set()
                for num, name, city_name, state_code, zip_code, lat, lon, normalized in sorted(rows, key=rank):
                    key = (num, normalized, lat, lon)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        'name': f'{num} {name}', 'kind': 'Street address',
                        'detail': ', '.join(part for part in (city_name, state_code, zip_code) if part),
                        'lat': lat, 'lon': lon, 'source': 'Overture Maps', 'onMap': True,
                    })
                    if len(results) == limit:
                        break
                return results
        return []
    finally:
        for connection in connections:
            connection.close()


# ---- Spain (and other countries that write the street before the number) ------------------------------------------
# Indexes named ES-<region>.sqlite are built by tools/build_addresses_es.py from Overture's Spanish rows (scne.es, CC BY 4.0).
# Their street names come as "CALLE CUEVAS LAS" or "AVINGUDA D'EMPURIES": a street type first, sometimes in Catalan,
# Galician or Basque, and articles moved to the end. People type "C/ de les Coves 12, Girona" or "Avda. Diagonal 640".
# Both sides are reduced to the words that name the street ("coves", "empuries", "diagonal"), so the type, the articles
# and the accents never decide a match; the house number, the town and the postcode still must.
ES_TYPES = set("""
calle c cl c/ carrer carrero calleja callejon carreron av avda avd avenida avinguda av. avgda avgda. plaza pza pl placa plaça
paseo passeig pg ps pso camino cami cm ctra carretera ronda rda rambla rbla travesia travessia trav travessera pasaje passatge
ptge psje via urbanizacion urb diseminados partida sector lugar barrio barri grupo raval veinat masias glorieta gta costa
carreró baixada bajada pujada subida cuesta
""".split())
ES_SMALL = set("de del dels d l la las los les el els en na sa ses s i y a al".split())
# The same kind of street in Spanish, Catalan and the usual abbreviations, so "Passeig de Gràcia" prefers "PASEO GRACIA"
# to "TRAVESSERA GRACIA", and "Rambla de Catalunya" prefers "RAMBLA CATALUNYA" to "AVINGUDA CATALUNYA".
ES_KIND = {w: kind for kind, words in {
    'calle': 'calle c cl carrer carrero calleja callejon carreron', 'avenida': 'av avda avd avenida avinguda avgda',
    'plaza': 'plaza pza pl placa', 'paseo': 'paseo passeig pg ps pso', 'camino': 'camino cami cm',
    'carretera': 'ctra carretera', 'ronda': 'ronda rda', 'rambla': 'rambla rbla', 'travesia': 'travesia travessia trav',
    'travessera': 'travessera', 'pasaje': 'pasaje passatge ptge psje', 'via': 'via', 'urbanizacion': 'urbanizacion urb',
    'glorieta': 'glorieta gta', 'baixada': 'baixada bajada', 'pujada': 'pujada subida cuesta costa'}.items()
    for w in words.split()}


def es_kind(street):
    """'Passeig de Gràcia' -> 'paseo'; '' when the street names no type."""
    words = fold(street).split()
    return ES_KIND.get(words[0], '') if len(words) > 1 else ''


def fold(text):
    """Lower case without accents: 'Plaça de l'Àngel' -> 'placa de l angel'. 'l·l' becomes 'll', 'ñ' 'n', 'ç' 'c'."""
    text = unicodedata.normalize('NFKD', text.lower().replace('l·l', 'll').replace('l.l', 'll'))
    return re.sub(r'[^a-z0-9 ]+', ' ', ''.join(c for c in text if not unicodedata.combining(c))).strip()


def es_norm(street):
    """The words that name a Spanish street, in order: 'CALLE CUEVAS LAS' and 'C/ de les Coves' -> 'cuevas', 'coves'."""
    words = fold(street).split()
    while len(words) > 1 and words[0] in ES_TYPES:
        words = words[1:]
    kept = [w for w in words if w not in ES_SMALL]
    return ' '.join(kept or words)


def es_display(street):
    """'CALLE CUEVAS LAS' -> 'Calle Las Cuevas': title case, and the article Overture moved to the end put back."""
    words = street.replace(',', ' ').split()
    if len(words) > 2 and fold(words[-1]) in {'el', 'la', 'las', 'los', 'les', 'els'}:
        words = [words[0], words[-1]] + words[1:-1]
    return ' '.join(w.capitalize() if not re.match(r"^[a-zà-ÿ]'", w, re.I) else w[:2].lower() + w[2:].capitalize()
                    for w in words)


def es_indexes(geo_directory):
    return sorted((Path(geo_directory) / 'addresses').glob('ES-*.sqlite'))


ES_NUMBER = re.compile(r'(\d{1,5})\s*([a-z])?$')


def es_parse(query):
    """(number, street words, town words, postcode) for 'Carrer de Mallorca 401, Barcelona 08013', or None.
    The number may also come first ('401 Carrer de Mallorca')."""
    tokens = fold(query.replace(',', ' , ')).split()
    postcode = ''
    for i, t in enumerate(tokens):
        if re.fullmatch(r'[0-5]\d{4}', t):
            postcode = tokens.pop(i)
            break
    words = [t for t in tokens]
    if words and re.fullmatch(r'\d{1,5}[a-z]?', words[0]) and len(words) > 1:
        m = re.fullmatch(r'(\d{1,5})([a-z])?', words[0])
        street, town = words[1:], []
    else:
        at = next((i for i, t in enumerate(words) if i > 0 and re.fullmatch(r'\d{1,5}[a-z]?', t)), None)
        if at is None:
            return None
        m = re.fullmatch(r'(\d{1,5})([a-z])?', words[at])
        street, town = words[:at], words[at + 1:]
        if town and len(town[0]) == 1 and town[0].isalpha() and town[0] not in ES_SMALL:   # '12 b'
            m = re.fullmatch(r'(\d{1,5})([a-z])?', m[1] + town[0])
            town = town[1:]
    number = m[1] + (m[2] or '').upper()
    street = [w for w in street if w != ',']
    town = [w for w in town if w not in (',', 'bis')]
    return number, street, town, postcode


def is_es_address_query(query, geo_directory):
    return bool(es_indexes(geo_directory)) and es_parse(query) is not None and bool(es_parse(query)[1])


def find_es_addresses(query, geo_directory, limit=8, near=None):
    """Exact house numbers from the installed Spanish indexes; never a guessed house. [] when nothing matches."""
    parsed = es_parse(query)
    if not parsed:
        return []
    number, street_words, town_words, postcode = parsed
    # Town words can also end the street ("Carrer Gran de Gracia 12" has none; "Mallorca 401 Barcelona" has one):
    # try the whole street first, then let trailing words stand for the town only when no town was typed after the number.
    readings = [(street_words, town_words)]
    if not town_words:
        readings += [(street_words[:k], street_words[k:]) for k in range(len(street_words) - 1, 0, -1)]
    results = []
    for path in es_indexes(geo_directory):
        try:
            db = sqlite3.connect(ro_uri(path), uri=True)
        except sqlite3.Error:
            continue
        try:
            for street, town in readings:
                norm = es_norm(' '.join(street))
                if not norm or norm in ES_TYPES:
                    continue
                town_norm = fold(' '.join(town))
                conditions = ['norm >= ?', 'norm < ?', '(num = ? OR num GLOB ?)']
                parameters = [norm, norm + '\uffff', number, number + '[A-Z]' if number[-1].isdigit() else number]
                if postcode:
                    conditions.append('zip = ?')
                    parameters.append(postcode)
                rows = db.execute('SELECT num,street,city,state,zip,lat,lon,norm,cityf FROM addr WHERE ' + ' AND '.join(conditions)
                                  + ' ORDER BY length(norm),norm LIMIT 400', parameters).fetchall()
                if town_norm:
                    rows = [r for r in rows if r[8] == town_norm or r[8].startswith(town_norm + ' ')
                            or (' ' + town_norm + ' ') in (' ' + r[8] + ' ') or r[8].startswith(town_norm)]
                exact = [r for r in rows if r[7] == norm]
                rows = exact or rows
                kind = es_kind(' '.join(street))
                same = [r for r in rows if es_kind(r[1]) == kind] if kind else []
                rows = same or rows
                if rows:
                    results += rows
                    break
        except sqlite3.Error:
            pass
        finally:
            db.close()
    def rank(r):
        d = 0
        if near:
            d = (r[5] - near[0]) ** 2 + ((r[6] - near[1]) * math.cos(math.radians(near[0]))) ** 2
        return r[0] != number, len(r[7]), d
    out, seen = [], set()
    for num, street, city, region, zip_code, lat, lon, norm, _ in sorted(results, key=rank):
        key = (num, norm, round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        out.append({'name': f'{es_display(street)} {num}', 'kind': 'Street address',
                    'detail': ', '.join(part for part in (f'{zip_code} {city}'.strip(), region) if part),
                    'lat': lat, 'lon': lon, 'source': 'Overture Maps (scne.es)', 'onMap': True})
        if len(out) == limit:
            break
    return out
