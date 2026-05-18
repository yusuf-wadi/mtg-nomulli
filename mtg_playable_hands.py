#!/usr/bin/env python3
import json, os, random, re, sys, urllib.request
from pathlib import Path

BULK_API = 'https://api.scryfall.com/bulk-data'
CACHE_DIR = Path('/tmp/mtg_cache')
BULK_PATH = CACHE_DIR / 'oracle_cards.json'
INDEX_PATH = CACHE_DIR / 'oracle_cards_index.json'
VERSION_PATH = CACHE_DIR / 'cache_version.txt'

# Bump this whenever index-building logic changes
CACHE_VERSION = '3'

SUBTYPE_MANA = {
    'plains':   'W',
    'island':   'U',
    'swamp':    'B',
    'mountain': 'R',
    'forest':   'G',
}


def normalize_name(raw: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'\([^)]*\)', '', re.sub(r'^[0-9]+x?\s+', '', raw.lower()))).strip()


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'MTGPlayableHands/1.0'})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def infer_produced_from_type(type_line: str):
    tl = (type_line or '').lower()
    produced = []
    after_dash = tl.split('\u2014')[-1] if '\u2014' in tl else tl.split('-')[-1]
    for sub in after_dash.split():
        color = SUBTYPE_MANA.get(sub.strip())
        if color and color not in produced:
            produced.append(color)
    return produced


def cache_is_valid() -> bool:
    if not (BULK_PATH.exists() and INDEX_PATH.exists() and VERSION_PATH.exists()):
        return False
    return VERSION_PATH.read_text().strip() == CACHE_VERSION


def ensure_bulk_data():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if cache_is_valid():
        with open(INDEX_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    # Stale or missing — rebuild
    manifest = fetch_json(BULK_API)
    bulk = next((x for x in manifest.get('data', []) if x.get('type') == 'oracle_cards'), None)
    if not bulk:
        raise RuntimeError('oracle_cards bulk dataset not found')
    data = fetch_json(bulk['download_uri'])
    with open(BULK_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    idx = {}
    for card in data:
        names = {normalize_name(card.get('name', ''))}
        for face in card.get('card_faces', []) or []:
            if face.get('name'):
                names.add(normalize_name(face['name']))
        type_line = card.get('type_line', '')
        produced = list(card.get('produced_mana') or [])
        if not produced and 'land' in type_line.lower():
            produced = infer_produced_from_type(type_line)
        compact = {
            'name': card.get('name', ''),
            'mana_cost': card.get('mana_cost', ''),
            'cmc': card.get('cmc', 0),
            'type_line': type_line,
            'colors': card.get('colors') or [],
            'color_identity': card.get('color_identity') or [],
            'produced_mana': produced,
            'card_faces': card.get('card_faces') or []
        }
        for n in names:
            idx.setdefault(n, compact)
    with open(INDEX_PATH, 'w', encoding='utf-8') as f:
        json.dump(idx, f)
    VERSION_PATH.write_text(CACHE_VERSION)
    return idx


def parse_mana_cost_symbols(mana_cost: str):
    counts = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0, 'generic': 0}
    for sym in re.findall(r'\{([^}]+)\}', mana_cost or ''):
        s = sym.upper()
        if s.isdigit(): counts['generic'] += int(s)
        elif s in counts: counts[s] += 1
        elif '/' in s:
            parts = [p for p in s.split('/') if p in counts]
            if len(parts) == 1: counts[parts[0]] += 1
            else: counts['generic'] += 1
        elif s not in {'X', 'Y', 'Z'}:
            counts['generic'] += 1
    return counts


def summarize_face_data(card):
    face = (card.get('card_faces') or [None])[0]
    type_line = card.get('type_line') or (face or {}).get('type_line', '')
    produced = list(card.get('produced_mana') or [])
    if not produced and 'land' in type_line.lower():
        produced = infer_produced_from_type(type_line)
    return {
        'mana_cost': card.get('mana_cost') or (face or {}).get('mana_cost', ''),
        'cmc': card.get('cmc', (face or {}).get('cmc', 0)) or 0,
        'type_line': type_line,
        'colors': card.get('colors') or (face or {}).get('colors', []) or [],
        'color_identity': card.get('color_identity') or [],
        'produced_mana': produced
    }


def classify_card(entry, card):
    face = summarize_face_data(card)
    type_line = face['type_line'].lower()
    is_land = 'land' in type_line
    is_permanent = any(x in type_line for x in ['artifact', 'creature', 'enchantment', 'planeswalker', 'battle', 'land'])
    produced = [c for c in face['produced_mana'] if c in ['W', 'U', 'B', 'R', 'G', 'C']]
    if not produced and is_land:
        produced = infer_produced_from_type(face['type_line'])
    return {
        **entry,
        'name': card.get('name', entry['inputName']),
        'mana_cost': face['mana_cost'],
        'manaValue': face['cmc'],
        'type_line': face['type_line'],
        'colors': face['colors'],
        'color_identity': face['color_identity'],
        'produced_mana': list(dict.fromkeys(produced)),
        'isLand': is_land,
        'isPermanent': is_permanent,
        'isManaPermanent': is_permanent and (not is_land) and len(produced) > 0,
        'costSymbols': parse_mana_cost_symbols(face['mana_cost'])
    }


def parse_decklist(text: str):
    counts = {}
    for line in [x.strip() for x in text.splitlines() if x.strip()]:
        m = re.match(r'^(\d+)x?\s+(.*)$', line, flags=re.I)
        qty = int(m.group(1)) if m else 1
        name = (m.group(2) if m else line).strip()
        counts[name] = counts.get(name, 0) + qty
    cards = []
    for name, qty in counts.items():
        for _ in range(qty):
            cards.append({'inputName': name, 'normalized': normalize_name(name)})
    return cards


def can_pay_cost(cost, available):
    pool = dict(available)
    for c in ['W', 'U', 'B', 'R', 'G', 'C']:
        if pool.get(c, 0) < cost.get(c, 0):
            return False
        pool[c] = pool.get(c, 0) - cost.get(c, 0)
    return sum(pool.values()) >= cost.get('generic', 0)


def choose_land_production(lands, desired_costs):
    pool = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    for land in lands:
        opts = land['produced_mana'] if land['produced_mana'] else ['C']
        chosen = next((c for c in opts if desired_costs.get(c, 0) > 0), opts[0])
        pool[chosen] += 1
    return pool


def available_mana_for_turn(hand, battlefield, lands_played):
    lands_in_play = [c for c in hand if c['isLand']][:lands_played]
    desired = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    for c in [x for x in hand if not x['isLand']]:
        for color in ['W', 'U', 'B', 'R', 'G', 'C']:
            desired[color] += c['costSymbols'].get(color, 0)
    pool = choose_land_production(lands_in_play, desired)
    for perm in [x for x in battlefield if x['isManaPermanent']]:
        opts = perm['produced_mana'] if perm['produced_mana'] else ['C']
        chosen = next((c for c in opts if desired.get(c, 0) > 0), opts[0])
        pool[chosen] += 1
    return {'pool': pool, 'total': sum(pool.values())}


def castable_cards(hand, battlefield, lands_played):
    mana = available_mana_for_turn(hand, battlefield, lands_played)
    return [c for c in hand if (not c['isLand']) and c['manaValue'] <= mana['total'] and can_pay_cost(c['costSymbols'], mana['pool'])]


def evaluate_opening(deck, turns_seen=3):
    seen = deck[:7 + turns_seen]
    hand = list(seen)
    battlefield = []
    curve_ok = True
    has_play = False
    sequence = []
    for turn in range(1, 4):
        lands_played = min(turn, sum(1 for c in hand if c['isLand']))
        mana = available_mana_for_turn(hand, battlefield, lands_played)
        if mana['total'] < turn:
            curve_ok = False
        options = castable_cards(hand, battlefield, lands_played)
        options.sort(key=lambda c: ((2 if c['isManaPermanent'] else 0) + c['manaValue']), reverse=True)
        playable = [c for c in options if c['manaValue'] <= turn]
        if playable:
            has_play = True
            chosen = playable[0]
            sequence.append({'turn': turn, 'card': chosen['name']})
            idx = next((i for i, h in enumerate(hand) if h['name'] == chosen['name'] and h['mana_cost'] == chosen['mana_cost']), -1)
            if idx >= 0:
                cast_card = hand.pop(idx)
                if cast_card['isPermanent']:
                    battlefield.append(cast_card)
    return {'playable': curve_ok and has_play, 'curveOk': curve_ok, 'hasPlayByTurn3': has_play, 'sequence': sequence}


def hydrate_deck(deck_text: str):
    index = ensure_bulk_data()
    parsed = parse_decklist(deck_text)
    resolved, missing = [], []
    for item in parsed:
        card = index.get(item['normalized'])
        if card: resolved.append(classify_card(item, card))
        else: missing.append(item['inputName'])
    return resolved, missing


def analyze(deck_text: str, simulations: int = 10000, turns_seen: int = 3):
    hydrated, missing = hydrate_deck(deck_text)
    if not hydrated:
        raise RuntimeError('No cards could be resolved from bulk data')
    playable = curve_ok = has_play = 0
    examples = []
    for i in range(simulations):
        deck = hydrated[:]
        random.shuffle(deck)
        res = evaluate_opening(deck, turns_seen=turns_seen)
        if res['playable']:
            playable += 1
            if len(examples) < 5:
                examples.append(res['sequence'])
        if res['curveOk']:
            curve_ok += 1
        if res['hasPlayByTurn3']:
            has_play += 1
    lands = sum(1 for c in hydrated if c['isLand'])
    mana_perms = sum(1 for c in hydrated if c['isManaPermanent'])
    nonlands = [c for c in hydrated if not c['isLand']]
    avg_mv = (sum(c['manaValue'] for c in nonlands) / len(nonlands)) if nonlands else 0
    colors = ''.join(sorted({x for c in hydrated for x in c.get('color_identity', [])})) or 'C'
    return {
        'deckSize': len(hydrated),
        'missing': missing,
        'colorIdentity': colors,
        'lands': lands,
        'manaPermanents': mana_perms,
        'averageNonlandManaValue': round(avg_mv, 4),
        'simulations': simulations,
        'turnsSeen': turns_seen,
        'results': {
            'playableHandsPct': round(playable / simulations * 100, 4),
            'onOrAboveCurveThroughTurn3Pct': round(curve_ok / simulations * 100, 4),
            'hasPlayableSpellByTurn3Pct': round(has_play / simulations * 100, 4)
        },
        'exampleSequences': examples
    }


if __name__ == '__main__':
    payload = json.loads(sys.stdin.read())
    result = analyze(payload.get('decklist', ''), int(payload.get('simulations', 10000)), int(payload.get('turns_seen', 3)))
    sys.stdout.write(json.dumps(result))
