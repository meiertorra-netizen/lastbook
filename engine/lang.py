"""Which language a question is written in: from its common short words, with no model and no download.

The library is in English. assistant.py uses this to search in English for a question written in another language and to
ask for the answer in the person's own language. Short or mixed text, and anything not clearly another of these
languages, counts as English, which is what the search would assume anyway.
"""
import re
import unicodedata

NAMES = {'en': 'English', 'es': 'Spanish', 'ca': 'Catalan', 'fr': 'French', 'de': 'German', 'it': 'Italian',
         'pt': 'Portuguese', 'nl': 'Dutch'}

# Words that are common in one language and rare in the others' questions (accents removed).
WORDS = {
    'en': 'the and is are how what when where why which who do does can should my your to of in on with for it this that i you if not have has from',
    'es': 'el los las del que como cuando donde por para con una uno es son mi mis tengo puedo debo hay esta este estoy agua sin muy hacer lo',
    'ca': 'el els les del que com quan on per amb una un es son meu meva tinc puc he hi ha aquest aquesta estic aigua sense molt fer ho no pas',
    'fr': 'le la les des que comment quand ou pour avec une un est sont mon ma mes je peux dois il y a ce cette suis eau sans tres faire pas',
    'de': 'der die das und ist sind wie was wann wo warum mit fur ein eine mein meine ich kann soll gibt es dieser diese bin wasser ohne sehr nicht',
    'it': 'il lo la gli le che come quando dove perche con una uno sono mio mia ho posso devo ci questo questa acqua senza molto fare non',
    'pt': 'o os as do da que como quando onde por para com uma um sao meu minha tenho posso devo ha este esta estou agua sem muito fazer nao',
    'nl': 'de het een en is zijn hoe wat wanneer waar waarom met voor mijn ik kan moet er dit deze ben water zonder erg doen niet',
}
SETS = {k: set(v.split()) for k, v in WORDS.items()}
MARKS = {'ca': ('l·l', 'ò'), 'es': ('ñ', '¿', '¡'), 'fr': ('ç', 'ê', 'è', 'à', "qu'"), 'de': ('ß', 'ä', 'ö', 'ü'),
         'pt': ('ã', 'õ', 'ç'), 'it': ("dell'", "un'")}


def detect(text):
    """'en', 'es', 'ca', 'fr', 'de', 'it', 'pt' or 'nl'."""
    low = (text or '').lower()
    plain = unicodedata.normalize('NFKD', low)
    plain = ''.join(c for c in plain if not unicodedata.combining(c))
    words = re.findall(r"[a-z]+", plain.replace("'", ' '))
    if len(words) < 3:
        return 'en'
    score = {k: sum(w in s for w in words) for k, s in SETS.items()}
    for k, marks in MARKS.items():
        score[k] += sum(2 for m in marks if m in low)
    best = max(score, key=score.get)
    if best == 'en' or score[best] < 2 or score[best] <= score['en']:
        return 'en'
    return best


def name(code):
    return NAMES.get(code, 'English')
