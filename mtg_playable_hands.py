#!/usr/bin/env python3
import json, os, random, re, sys, urllib.request
from pathlib import Path

BULK_API = 'https://api.scryfall.com/bulk-data'
CACHE_DIR = Path('/tmp/mtg_cache')
BULK_PATH = CACHE_DIR / 'oracle_cards.json'
INDEX_PATH = CACHE_DIR / 'oracle_cards_index.json'
VERSION_PATH = CACHE_DIR / 'cache_version.txt'
CACHE_VERSION = '8'  # bumped: cmc now stored directly from Scryfall top-level field

BASIC_LANDS = {
    'plains':   ['W'],
    'island':   ['U'],
    'swamp':    ['B'],
    'mountain': ['R'],
    'forest':   ['G'],
    'wastes':   ['C'],
}

SUBTYPE_MANA = {
    'plains':   'W',
    'island':   'U',
    'swamp':    'B',
    'mountain': 'R',
    'forest':   'G',
}

ENTERS_TAPPED_RE = re.compile(
    r'enters(?: the battlefield)? tapped',
    re.IGNORECASE
)


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


def _enters_tapped(card: dict) -> bool:
    texts = [card.get('oracle_text') or '']
    for face in card.get('card_faces') or []:
        texts.append(face.get('oracle_text') or '')
    return any(ENTERS_TAPPED_RE.search(t) for t in texts)


def cache_is_valid() -> bool:
    if not (BULK_PATH.exists() and INDEX_PATH.exists() and VERSION_PATH.exists()):
        return False
    return VERSION_PATH.read_text().strip() == CACHE_VERSION


def _card_mana_cost(card: dict) -> str:
    top = card.get('mana_cost') or ''
    if top:
        return top
    faces = card.get('card_faces') or []
    return (faces[0].get('mana_cost') or '') if faces else ''


def _card_cmc(card: dict) -> float:
    """Always read the top-level cmc field from Scryfall.
    For double-faced cards the top-level cmc is the front face's CMC.
    Never fall back to 0 silently — return 0 only if truly absent."""
    cmc = card.get('cmc')
    if cmc is not None:
        return float(cmc)
    faces = card.get('card_faces') or []
    if faces:
        face_cmc = faces[0].get('cmc')
        if face_cmc is not None:
            return float(face_cmc)
    return 0.0


def ensure_bulk_data():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if cache_is_valid():
        with open(INDEX_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
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
        if 'land' in type_line.lower():
            for c in infer_produced_from_type(type_line):
                if c not in produced:
                    produced.append(c)
        mana_cost = _card_mana_cost(card)
        cmc = _card_cmc(card)  # authoritative CMC from Scryfall
        compact = {
            'name': card.get('name', ''),
            'mana_cost': mana_cost,
            'cmc': cmc,
            'type_line': type_line,
            'colors': card.get('colors') or [],
            'color_identity': card.get('color_identity') or [],
            'produced_mana': produced,
            'card_faces': card.get('card_faces') or [],
            'entersTapped': _enters_tapped(card),
        }
        for n in names:
            idx.setdefault(n, compact)
    with open(INDEX_PATH, 'w', encoding='utf-8') as f:
        json.dump(idx, f)
    VERSION_PATH.write_text(CACHE_VERSION)
    return idx


def parse_mana_cost_symbols(mana_cost: str) -> dict:
    """Parse a Scryfall mana cost string like {2}{U} into symbol counts.
    Returns: {'W','U','B','R','G','C': int pips, 'generic': int generic mana}
    Example: {2}{U} -> generic=2, U=1, total CMC=3
    """
    counts = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0, 'generic': 0}
    for sym in re.findall(r'\{([^}]+)\}', mana_cost or ''):
        s = sym.upper()
        if s.isdigit():
            counts['generic'] += int(s)
        elif s in ('W', 'U', 'B', 'R', 'G', 'C'):
            counts[s] += 1
        elif s == 'X' or s == 'Y' or s == 'Z':
            pass  # variable costs ignored for castability checks
        elif '/' in s:
            # Hybrid or Phyrexian pip: {W/U}, {2/W}, {W/P}
            parts = s.split('/')
            color_parts = [p for p in parts if p in ('W', 'U', 'B', 'R', 'G', 'C')]
            numeric_parts = [int(p) for p in parts if p.isdigit()]
            if color_parts:
                # Treat as 1 generic (any of the options satisfies it)
                counts['generic'] += 1
            elif numeric_parts:
                counts['generic'] += numeric_parts[0]
            else:
                counts['generic'] += 1
        elif s == 'S':  # snow mana
            counts['generic'] += 1
        else:
            counts['generic'] += 1
    return counts


def cost_total(cost: dict) -> int:
    """Total mana needed to cast a spell (sum of all pips + generic)."""
    return sum(v for k, v in cost.items() if k != 'generic') + cost.get('generic', 0)


def summarize_face_data(card):
    face = (card.get('card_faces') or [None])[0]
    type_line = card.get('type_line') or (face or {}).get('type_line', '')
    produced = list(card.get('produced_mana') or [])
    if 'land' in type_line.lower():
        for c in infer_produced_from_type(type_line):
            if c not in produced:
                produced.append(c)
    mana_cost = card.get('mana_cost') or (face or {}).get('mana_cost', '') or ''
    # Always use the top-level authoritative cmc
    cmc = card.get('cmc')
    if cmc is None and face:
        cmc = face.get('cmc')
    cmc = float(cmc) if cmc is not None else 0.0
    return {
        'mana_cost': mana_cost,
        'cmc': cmc,
        'type_line': type_line,
        'colors': card.get('colors') or (face or {}).get('colors', []) or [],
        'color_identity': card.get('color_identity') or [],
        'produced_mana': produced,
        'entersTapped': card.get('entersTapped', False),
    }


def classify_card(entry, card):
    norm = entry['normalized']
    basic_colors = BASIC_LANDS.get(norm)
    if basic_colors is None:
        basic_colors = BASIC_LANDS.get((card.get('name') or '').lower().strip())
    if basic_colors is not None:
        type_line = card.get('type_line', 'Basic Land')
        return {
            **entry,
            'name': card.get('name', entry['inputName']),
            'mana_cost': '',
            'manaValue': 0,
            'type_line': type_line,
            'colors': [],
            'color_identity': basic_colors,
            'produced_mana': basic_colors,
            'isLand': True,
            'isPermanent': True,
            'isManaPermanent': False,
            'entersTapped': False,
            'costSymbols': parse_mana_cost_symbols(''),
        }
    face = summarize_face_data(card)
    type_line = face['type_line'].lower()
    is_land = 'land' in type_line
    is_permanent = any(x in type_line for x in ['artifact', 'creature', 'enchantment', 'planeswalker', 'battle', 'land'])
    produced = [c for c in face['produced_mana'] if c in ['W', 'U', 'B', 'R', 'G', 'C']]
    if not produced and is_land:
        produced = infer_produced_from_type(face['type_line'])
    cost_symbols = parse_mana_cost_symbols(face['mana_cost'])
    return {
        **entry,
        'name': card.get('name', entry['inputName']),
        'mana_cost': face['mana_cost'],
        'manaValue': face['cmc'],  # authoritative Scryfall CMC
        'type_line': face['type_line'],
        'colors': face['colors'],
        'color_identity': face['color_identity'],
        'produced_mana': list(dict.fromkeys(produced)),
        'isLand': is_land,
        'isPermanent': is_permanent,
        'isManaPermanent': is_permanent and (not is_land) and len(produced) > 0,
        'entersTapped': face['entersTapped'],
        'costSymbols': cost_symbols,
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


def can_pay_cost(cost: dict, pool: dict) -> bool:
    """Check if the mana pool can pay the cost.
    First satisfy all specific color pips, then check remaining mana >= generic.
    """
    remaining = dict(pool)
    for c in ('W', 'U', 'B', 'R', 'G', 'C'):
        need = cost.get(c, 0)
        have = remaining.get(c, 0)
        if have < need:
            return False
        remaining[c] = have - need
    generic = cost.get('generic', 0)
    return sum(remaining.values()) >= generic


def spend_mana(cost: dict, pool: dict) -> dict:
    """Deduct the cost from the pool. Colored pips come off their color;
    generic mana is taken from colorless first, then least-needed colors."""
    remaining = dict(pool)
    for c in ('W', 'U', 'B', 'R', 'G', 'C'):
        remaining[c] = remaining.get(c, 0) - cost.get(c, 0)
    generic = cost.get('generic', 0)
    # Spend generic from colorless first, then surplus colored (least desired order)
    for c in ('C', 'G', 'R', 'B', 'U', 'W'):
        if generic <= 0:
            break
        use = min(remaining.get(c, 0), generic)
        remaining[c] = remaining.get(c, 0) - use
        generic -= use
    return remaining


def build_mana_pool(lands_on_battlefield: list, mana_perms_on_battlefield: list, desired: dict) -> tuple:
    """Build a mana pool from available untapped sources.

    `desired` should map each color (W/U/B/R/G/C) to the total pips needed
    across all spells in hand PLUS the generic mana needed distributed to
    any color.  We use a two-pass approach:
      Pass 1 — assign each source to a color pip it can directly satisfy.
      Pass 2 — assign remaining sources to any color (extra generic coverage).
    This means a land that produces {W} or {U} will correctly contribute
    to a {2}{U} spell: pass 1 assigns it to U if U is needed, and the
    second unassigned source covers generic.
    """
    pool = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    sources_used = []
    sources = lands_on_battlefield + mana_perms_on_battlefield

    remaining_desired = dict(desired)

    for source in sources:
        opts = source.get('produced_mana') or ['C']
        # Try to satisfy a specific color pip first
        chosen = next((c for c in opts if remaining_desired.get(c, 0) > 0), None)
        if chosen:
            remaining_desired[chosen] = max(0, remaining_desired[chosen] - 1)
        else:
            # No specific pip needed — just pick the first available color
            chosen = opts[0]
        pool[chosen] = pool.get(chosen, 0) + 1
        sources_used.append(source['name'])

    return pool, sources_used


def desired_from_hand(hand: list) -> dict:
    """Sum up color requirements across all spells in hand.
    Generic mana is distributed: each point of generic adds 1 to 'C' bucket
    (colorless generic coverage) so that build_mana_pool allocates enough
    total mana even for {2}{U}-style costs.
    """
    desired = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    for card in hand:
        if card.get('isLand'):
            continue
        cost = card.get('costSymbols', {})
        for color in ('W', 'U', 'B', 'R', 'G', 'C'):
            desired[color] += cost.get(color, 0)
        # Generic mana: add to 'C' so we request enough total mana sources
        desired['C'] += cost.get('generic', 0)
    return desired


def evaluate_opening(deck: list, turns_seen: int = 3) -> dict:
    """
    Simulate turns 1-3 with a correct MTG state machine.

    Rules modelled:
    - Tapped lands enter tapped — they produce 0 mana the turn they are played.
      They untap at the start of the next turn (tapped_staging -> lands_in_play).
    - Mana permanents (Sol Ring, Signets, mana dorks) enter untapped and
      can tap for mana the same turn they are cast.
    - Color-pip requirements are checked precisely: {2}{U} requires exactly
      1 blue pip plus 2 generic, not 3 of any color.
    - Generic mana is paid from leftover colored mana after pips are satisfied.
    - The simulator prefers casting mana ramp on early turns to compound.
    """
    opening_hand = deck[:7]
    draw_pile = deck[7:]

    hand = list(opening_hand)
    lands_in_play = []        # untapped and available this turn
    tapped_staging = []       # played this turn as tapped; untap next turn
    mana_perms_in_play = []
    nonmana_perms_in_play = []

    curve_ok = True
    has_play = False
    turns = []

    for turn in range(1, 4):
        turn_log = {
            'turn': turn,
            'drew': None,
            'landPlayed': None,
            'landTapped': False,
            'manaPool': {},
            'manaSources': [],
            'cast': None
        }

        # Untap step: previously tapped lands are now available
        lands_in_play.extend(tapped_staging)
        tapped_staging = []

        # Draw (Commander: draw every turn including turn 1)
        if draw_pile:
            drawn = draw_pile.pop(0)
            hand.append(drawn)
            turn_log['drew'] = drawn['name']

        # Play a land — prefer untapped lands for early turns
        land_candidates = [c for c in hand if c['isLand']]
        if land_candidates:
            desired = desired_from_hand(hand)

            def land_score(land):
                opts = land.get('produced_mana') or ['C']
                color_value = sum(desired.get(c, 0) for c in opts)
                # Heavily penalise tapped lands so we play untapped first
                tapped_penalty = -1000 if land.get('entersTapped') else 0
                return color_value + tapped_penalty

            land_to_play = max(land_candidates, key=land_score)
            hand.remove(land_to_play)
            enters_tapped = land_to_play.get('entersTapped', False)
            if enters_tapped:
                tapped_staging.append(land_to_play)
            else:
                lands_in_play.append(land_to_play)
            turn_log['landPlayed'] = land_to_play['name']
            turn_log['landTapped'] = enters_tapped

        # Build mana pool from currently untapped lands + mana perms in play
        desired_pool = desired_from_hand(hand)
        pool, sources = build_mana_pool(lands_in_play, mana_perms_in_play, desired_pool)
        turn_log['manaPool'] = {k: v for k, v in pool.items() if v > 0}
        turn_log['manaSources'] = sources
        total_mana = sum(pool.values())

        if total_mana < turn:
            curve_ok = False

        # Find castable spells: must satisfy both color pips AND generic mana
        castable = [
            c for c in hand
            if not c['isLand']
            and can_pay_cost(c['costSymbols'], pool)
        ]
        # Prioritise: mana ramp first, then by MV descending (biggest bang for available mana)
        castable.sort(
            key=lambda c: (2 if c['isManaPermanent'] else 0) + c['manaValue'],
            reverse=True
        )

        if castable:
            has_play = True
            chosen = castable[0]
            mv = int(chosen['manaValue']) if chosen['manaValue'] == int(chosen['manaValue']) else chosen['manaValue']
            turn_log['cast'] = {
                'name': chosen['name'],
                'manaCost': chosen['mana_cost'],
                'mv': mv
            }
            hand.remove(chosen)
            pool = spend_mana(chosen['costSymbols'], pool)
            if chosen['isPermanent']:
                if chosen['isManaPermanent']:
                    mana_perms_in_play.append(chosen)
                else:
                    nonmana_perms_in_play.append(chosen)

        turns.append(turn_log)

    return {
        'playable': curve_ok and has_play,
        'curveOk': curve_ok,
        'hasPlayByTurn3': has_play,
        'openingHand': [c['name'] for c in opening_hand],
        'turns': turns,
    }


def hydrate_deck(deck_text: str):
    index = ensure_bulk_data()
    parsed = parse_decklist(deck_text)
    resolved, missing = [], []
    for item in parsed:
        if item['normalized'] in BASIC_LANDS:
            stub = {
                'name': item['inputName'].strip(),
                'type_line': 'Basic Land',
                'mana_cost': '', 'cmc': 0,
                'colors': [],
                'color_identity': BASIC_LANDS[item['normalized']],
                'produced_mana': BASIC_LANDS[item['normalized']],
                'card_faces': [],
                'entersTapped': False,
            }
            resolved.append(classify_card(item, stub))
            continue
        card = index.get(item['normalized'])
        if card:
            resolved.append(classify_card(item, card))
        else:
            missing.append(item['inputName'])
    return resolved, missing


def analyze(deck_text: str, simulations: int = 10000, turns_seen: int = 3):
    hydrated, missing = hydrate_deck(deck_text)
    if not hydrated:
        raise RuntimeError('No cards could be resolved from bulk data')
    playable_count = curve_ok_count = has_play_count = 0
    examples = []
    for _ in range(simulations):
        deck = hydrated[:]
        random.shuffle(deck)
        res = evaluate_opening(deck, turns_seen=turns_seen)
        if res['playable']:
            playable_count += 1
            if len(examples) < 6:
                examples.append({
                    'openingHand': res['openingHand'],
                    'turns': res['turns'],
                })
        if res['curveOk']:
            curve_ok_count += 1
        if res['hasPlayByTurn3']:
            has_play_count += 1
    lands = sum(1 for c in hydrated if c['isLand'])
    mana_perms = sum(1 for c in hydrated if c['isManaPermanent'])
    nonlands = [c for c in hydrated if not c['isLand']]
    avg_mv = (sum(c['manaValue'] for c in nonlands) / len(nonlands)) if nonlands else 0
    colors = ''.join(sorted({x for c in hydrated for x in c.get('color_identity', [])})) or 'C'
    tapped_land_count = sum(1 for c in hydrated if c.get('isLand') and c.get('entersTapped'))
    return {
        'deckSize': len(hydrated),
        'missing': missing,
        'colorIdentity': colors,
        'lands': lands,
        'tappedLands': tapped_land_count,
        'manaPermanents': mana_perms,
        'averageNonlandManaValue': round(avg_mv, 4),
        'simulations': simulations,
        'turnsSeen': turns_seen,
        'results': {
            'playableHandsPct': round(playable_count / simulations * 100, 4),
            'onOrAboveCurveThroughTurn3Pct': round(curve_ok_count / simulations * 100, 4),
            'hasPlayableSpellByTurn3Pct': round(has_play_count / simulations * 100, 4)
        },
        'exampleSequences': examples
    }


if __name__ == '__main__':
    payload = json.loads(sys.stdin.read())
    result = analyze(payload.get('decklist', ''), int(payload.get('simulations', 10000)), int(payload.get('turns_seen', 3)))
    sys.stdout.write(json.dumps(result))
