"""
Topic bank for content pages: real, documented places and objects, each with
its English Wikipedia title so the facts (and the photo) come from a source
instead of the model's imagination. Users add their own under Profile.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime

import requests

from config.db import get_connection

log = logging.getLogger("topics")
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
USER_AGENT = "SocialAutomation/1.0 (self-hosted social media tool)"
TIMEOUT = 30

# (English name for the post and photo search, Wikipedia title)
BANK = [
    # Greece
    ("Dragon Houses of Euboea", "Drakospita"),
    ("Lion Gate of Mycenae", "Lion Gate"),
    ("Treasury of Atreus", "Treasury of Atreus"),
    ("Cyclopean walls of Tiryns", "Tiryns"),
    ("Antikythera mechanism", "Antikythera mechanism"),
    ("Pyramid of Hellinikon", "Pyramid of Hellinikon"),
    ("Theopetra Cave wall", "Theopetra cave"),
    ("Sanctuary of the Great Gods, Samothrace", "Samothrace temple complex"),
    ("Oracle of the Dead at Ephyra", "Necromanteion of Acheron"),
    ("Temple of Apollo at Delphi", "Temple of Apollo (Delphi)"),
    ("Minoan palace of Knossos", "Knossos"),
    ("Phaistos Disc", "Phaistos Disc"),
    ("Akrotiri on Santorini", "Akrotiri (prehistoric city)"),
    ("Cave of Zeus on Mount Ida", "Idaean Cave"),
    ("Melissani Cave", "Melissani Cave"),
    ("Meteora monasteries", "Meteora"),
    ("Epidaurus theatre acoustics", "Ancient Theatre of Epidaurus"),
    ("Palaikastro Kouros", "Palaikastro Kouros"),
    ("Dispilio tablet", "Dispilio Tablet"),
    ("Mount Olympus", "Mount Olympus"),
    ("Sesklo and Dimini", "Dimini"),
    ("Lake Vouliagmeni", "Lake Vouliagmeni"),
    ("Cave of the Lakes", "Cave of the Lakes"),
    ("Monemvasia rock", "Monemvasia"),
    ("Ancient Messene", "Messene"),
    ("Mystras", "Mystras"),
    ("Acropolis of Lindos, Rhodes", "Lindos"),
    ("Colossus of Rhodes", "Colossus of Rhodes"),
    ("Kameiros, Rhodes", "Kameiros"),
    ("Petaloudes Valley, Rhodes", "Petaloudes, Rhodes"),
    ("Asklepieion of Kos", "Asklepieion of Kos"),
    ("Delos", "Delos"),
    ("Mount Athos", "Mount Athos"),
    ("Vergina royal tombs", "Vergina"),
    ("Pella mosaics", "Pella"),
    ("Dodona oracle", "Dodona"),
    ("Mycenaean Nestor's Palace", "Palace of Nestor"),
    ("Gortyn code", "Gortyn code"),
    ("Kerameikos", "Kerameikos"),
    ("Eupalinos tunnel on Samos", "Tunnel of Eupalinos"),
    ("Sounion temple of Poseidon", "Cape Sounion"),
    # Megaliths and ancient engineering worldwide
    ("Baalbek trilithon stones", "Baalbek"),
    ("Stone of the Pregnant Woman, Baalbek", "Stone of the Pregnant Woman"),
    ("Sacsayhuaman walls", "Sacsayhuamán"),
    ("Ollantaytambo", "Ollantaytambo"),
    ("Puma Punku", "Pumapunku"),
    ("Tiwanaku Gate of the Sun", "Gate of the Sun"),
    ("Machu Picchu", "Machu Picchu"),
    ("Nazca Lines", "Nazca Lines"),
    ("Gobekli Tepe", "Göbekli Tepe"),
    ("Karahan Tepe", "Karahan Tepe"),
    ("Derinkuyu underground city", "Derinkuyu underground city"),
    ("Stonehenge", "Stonehenge"),
    ("Avebury stone circle", "Avebury"),
    ("Carnac stones", "Carnac stones"),
    ("Newgrange", "Newgrange"),
    ("Callanish stones", "Calanais Stones"),
    ("Ggantija temples, Malta", "Ġgantija"),
    ("Hypogeum of Hal Saflieni", "Ħal Saflieni Hypogeum"),
    ("Great Sphinx of Giza", "Great Sphinx of Giza"),
    ("Great Pyramid of Giza", "Great Pyramid of Giza"),
    ("Serapeum of Saqqara", "Serapeum of Saqqara"),
    ("Osireion at Abydos", "Osireion"),
    ("Unfinished obelisk of Aswan", "Unfinished obelisk"),
    ("Petra", "Petra"),
    ("Nan Madol", "Nan Madol"),
    ("Moai of Easter Island", "Moai"),
    ("Yonaguni monument", "Yonaguni Monument"),
    ("Longyou caves", "Longyou Caves"),
    ("Olmec colossal heads", "Olmec colossal heads"),
    ("Teotihuacan Pyramid of the Sun", "Pyramid of the Sun"),
    ("Chichen Itza", "Chichen Itza"),
    ("Plain of Jars, Laos", "Plain of Jars"),
    ("Carahunge, Armenia", "Carahunge"),
    ("Stone spheres of Costa Rica", "Stone spheres of Costa Rica"),
    ("Mohenjo-daro", "Mohenjo-daro"),
    ("Great Zimbabwe", "Great Zimbabwe"),
    ("Angkor Wat", "Angkor Wat"),
    ("Borobudur", "Borobudur"),
    ("Gunung Padang", "Gunung Padang"),
    ("Coral Castle, Florida", "Coral Castle"),
    ("Mount Nemrut statues", "Mount Nemrut"),
    ("Hagia Sophia dome", "Hagia Sophia"),
    ("Pantheon dome, Rome", "Pantheon, Rome"),
    ("Lalibela rock churches", "Lalibela"),
    ("Kailasa temple, Ellora", "Kailasa Temple, Ellora"),
    ("Mesa Verde cliff dwellings", "Mesa Verde National Park"),
    ("Chaco Canyon", "Chaco Culture National Historical Park"),
    ("Skara Brae", "Skara Brae"),
    ("Dolmen of Menga", "Dolmen de Menga"),
    ("Externsteine", "Externsteine"),
    ("Bada Valley megaliths", "Bada Valley"),
    ("Mount Shoria megaliths", "Mount Shoria megaliths") if False else ("Rujm el-Hiri", "Rujm el-Hiri"),
    ("Ciudad Perdida", "Ciudad Perdida"),
    ("Cahokia Mounds", "Cahokia"),
    ("Lake Titicaca", "Lake Titicaca"),
    ("Richat Structure", "Richat Structure"),
    ("Devils Tower", "Devils Tower"),
    ("Giant's Causeway", "Giant's Causeway"),
    # Strange objects, manuscripts, maps
    ("Voynich manuscript", "Voynich manuscript"),
    ("Piri Reis map", "Piri Reis map"),
    ("Baghdad Battery", "Baghdad Battery"),
    ("Nebra sky disc", "Nebra sky disc"),
    ("Shroud of Turin", "Shroud of Turin"),
    ("Crystal skull, British Museum", "Crystal skull"),
    ("Dropa stones", "Dropa stones"),
    ("Lady of Elche", "Lady of Elche"),
    ("Sumerian King List", "Sumerian King List"),
    ("Dead Sea Scrolls", "Dead Sea Scrolls"),
    ("Codex Gigas, the Devil's Bible", "Codex Gigas"),
    ("Rosetta Stone", "Rosetta Stone"),
    ("Linear A script", "Linear A"),
    ("Kensington Runestone", "Kensington Runestone"),
    # UFOs and the sky
    ("Roswell incident", "Roswell incident"),
    ("Area 51", "Area 51"),
    ("Rendlesham Forest incident", "Rendlesham Forest incident"),
    ("Phoenix Lights", "Phoenix Lights"),
    ("Kecksburg UFO incident", "Kecksburg UFO incident"),
    ("Pentagon UFO videos", "Pentagon UFO videos"),
    ("Hessdalen lights", "Hessdalen lights"),
    ("Marfa lights", "Marfa lights"),
    ("Wow! signal", "Wow! signal"),
    ("Oumuamua interstellar object", "ʻOumuamua"),
    ("Tunguska event", "Tunguska event"),
    ("Chelyabinsk meteor", "Chelyabinsk meteor"),
    ("Hoia Forest", "Hoia Forest"),
    ("Betz mystery sphere", "Betz mystery sphere"),
    ("Varginha UFO incident", "Varginha UFO incident"),
    ("Tehran UFO incident", "1976 Tehran UFO incident"),
    ("Westall UFO", "Westall UFO"),
    # Strange places and natural mysteries
    ("Sailing stones of Death Valley", "Sailing stones"),
    ("Blood Falls, Antarctica", "Blood Falls"),
    ("Darvaza gas crater, the Door to Hell", "Darvaza gas crater"),
    ("Eternal Flame Falls", "Eternal Flame Falls"),
    ("Catatumbo lightning", "Catatumbo lightning"),
    ("Naga fireballs", "Naga fireball"),
    ("Fairy circles of Namibia", "Fairy circle (arid grass formation)"),
    ("Moeraki boulders", "Moeraki Boulders"),
    ("Cave of the Crystals, Naica", "Cave of the Crystals"),
    ("Zone of Silence, Mexico", "Mapimí Silent Zone"),
    ("Oak Island money pit", "Oak Island mystery"),
    ("Bermuda Triangle", "Bermuda Triangle"),
    ("Dyatlov Pass incident", "Dyatlov Pass incident"),
    ("Mary Celeste", "Mary Celeste"),
    ("Devil's Kettle waterfall", "Judge C. R. Magney State Park"),
    ("Lake Natron", "Lake Natron"),
    ("Socotra dragon blood trees", "Socotra"),
    ("Chocolate Hills", "Chocolate Hills"),
    ("Pamukkale terraces", "Pamukkale"),
    ("Spotted Lake, Canada", "Spotted Lake"),
    ("Racetrack Playa", "Racetrack Playa"),
    ("Bimini Road", "Bimini Road"),
    ("Underwater city of Pavlopetri", "Pavlopetri"),
    ("Sunken city of Olous, Crete", "Olous"),
    ("Thonis-Heracleion", "Heracleion"),
    ("Lost city of Dwarka", "Dwarka"),
    ("Zealandia, the hidden continent", "Zealandia"),
    ("Ball's Pyramid", "Ball's Pyramid"),
    ("Movile Cave", "Movile Cave"),
    ("Kawah Ijen blue fire", "Ijen"),
    ("Yellowstone supervolcano", "Yellowstone Caldera"),
    ("Santorini caldera and the Minoan eruption", "Minoan eruption"),
    ("Nisyros volcano", "Nisyros"),
    ("Cave of Diros", "Diros Caves"),
    ("Kerlingarfjoll", "Kerlingarfjöll"),
]


def wiki_summary(title: str) -> dict:
    """{'extract': str, 'title': str, 'url': str} from Wikipedia's summary endpoint, or {}."""
    try:
        resp = requests.get(WIKI_SUMMARY.format(title=title.replace(" ", "_")),
                            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT)
        if resp.status_code != 200:
            return {}
        data = resp.json()
    except (requests.RequestException, ValueError):
        return {}
    if data.get("type") == "disambiguation":
        return {}
    return {"extract": (data.get("extract") or "").strip(), "title": data.get("title") or title,
            "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page") or "",
            "image": ((data.get("originalimage") or {}).get("source")) or ""}


def user_topics(user_id: int) -> list[tuple[str, str]]:
    """Topics the user typed on the Profile page: 'Name' or 'Name | Wikipedia title', one per line."""
    with get_connection() as conn:
        row = conn.execute("SELECT topic_bank FROM users WHERE id = ?", (user_id,)).fetchone()
    out = []
    for line in ((row["topic_bank"] if row else "") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, title = line.partition("|")
        out.append((name.strip(), (title.strip() or name.strip())))
    return out


def used_refs(user_id: int) -> set[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT source_ref FROM posts WHERE user_id = ? AND source_ref IS NOT NULL", (user_id,)).fetchall()
    return {r["source_ref"] for r in rows}


def pick(user_id: int, count: int, rng: random.Random | None = None) -> list[tuple[str, str]]:
    """`count` topics not yet used by this user: the user's own first, then the bank,
    shuffled so two users (or two days) do not get the same order."""
    rng = rng or random.Random(f"{user_id}-{datetime.now():%Y-%m-%d}")
    used = used_refs(user_id)
    own = [t for t in user_topics(user_id) if t[1] not in used]
    bank = [t for t in BANK if t[1] not in used and t not in own]
    rng.shuffle(bank)
    chosen = (own + bank)[:count]
    if len(chosen) < count:                 # everything used once: start over with the least recent
        log.info("topic bank exhausted for user %s; reusing", user_id)
        rest = [t for t in BANK if t not in chosen]
        rng.shuffle(rest)
        chosen += rest[:count - len(chosen)]
    return chosen
