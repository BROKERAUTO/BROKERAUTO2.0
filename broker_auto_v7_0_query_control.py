#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
              BROKER AUTO AI v7.0.1 - QUERY CONTROL + COERENZA DATI
================================================================================

CHANGELOG v7.0:

NUOVE FUNZIONALITÀ:

1. CONTROLLO QUERY UTENTE:
   - L'utente può specificare quante query lanciare (non più "20 fisso")
   - L'utente può RIVEDERE l'elenco query prima dello scraping
   - L'utente può ESCLUDERE query specifiche
   - L'utente può MODIFICARE singole query

2. QUERY ALLINEATE ALLE CARLINE:
   - Ogni query è legata a marca+modello specifici
   - Validazione coerenza URL ↔ query richiesta
   - Scarto immediato di risultati non pertinenti

3. COERENZA DATI GARANTITA:
   - Struttura dati UNICA per lead (no liste parallele)
   - Ogni riga CSV = UNA auto
   - Ogni card HTML = UNA auto con link corretto
   - Niente più "BMW X3 - Citroën C3" come singola vettura

================================================================================

CORREZIONI v7.0.1 (Review integrativa):

FIX CRITICI:
- [FIX] Validazione titolo post-scrape: verifica che il titolo dell'annuncio
  non contenga una marca DIVERSA da quella cercata (evita "BMW X3" salvato
  come Audi perché l'URL sembrava corretto ma l'annuncio era di altra marca)

FIX FILTRI:
- [FIX] FiltroPrezziFalsi esteso: aggiunte 20+ keywords per rilevare prezzi
  promozionali (rata mensile, TAN/TAEG, finanziamento, rottamazione, ecc.)
- [FIX] Pattern regex per "da X€/mese" e "a partire da X€"

FIX DATI:
- [FIX] prezzo_richiesto ora correttamente inizializzato (era sempre 0)
- [FIX] Estrazione telefono implementata (era nei selettori ma non estratto)
- [FIX] CSV arricchito con titolo, segmento e scostamento_percentuale

FIX UX:
- [FIX] Comando '+' per riattivare query singole (prima solo 'all')
- [FIX] Comando 'list' per rivedere elenco query durante revisione
- [FIX] Stima lead realistica nel riepilogo (considera filtri ~40-70%)
- [FIX] Conferma esplicita prima dello scraping (S/n invece di solo ENTER)
- [FIX] Report finale con statistiche qualità (telefono, priorità)

================================================================================
"""

import sys, os, asyncio, json, csv, base64, random, time, re, logging, hashlib
from datetime import datetime
from io import BytesIO
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import quote_plus, urljoin
from dataclasses import dataclass, field, asdict

# ============================================================================
# CONFIGURAZIONE PRINCIPALE
# ============================================================================

CONFIG_PREZZO_MIN = 20000
CONFIG_PREZZO_MAX = 75000
CONFIG_KM_MAX = 150000
CONFIG_ANNO_MIN = 2016

# Portali attivi
PORTALI_ATTIVI = ["autoscout24", "automobile"]

# FIX QUERY CONTROL: Limite query configurabile (non più hardcoded)
DEFAULT_MAX_QUERIES = 30  # Valore di default, modificabile dall'utente
MAX_LEAD_PER_QUERY = 10   # Lead massimi per singola query
CONFIG_MAX_LEAD_TOTALI = 500

# Delay e timeout
CONFIG_DELAY_MIN = 4
CONFIG_DELAY_MAX = 8
CONFIG_DELAY_TRA_PIATTAFORME = 3
CONFIG_DELAY_TRA_QUERY = 8

CONFIG_TIMEOUT_NAVIGAZIONE = 45000
CONFIG_TIMEOUT_ELEMENTO = 8000
CONFIG_MAX_RETRY = 2
CONFIG_MAX_LEAD_DASHBOARD = 300
CONFIG_OUTPUT_DIR = '.'

# Conto vendita
CONFIG_PROVVIGIONE_CONTO_VENDITA = 0.04

# Debug
DEBUG_MODE = True
SAVE_SCREENSHOTS = False

# ============================================================================
# FIX HTML COHERENCE: Dataclass per Lead (struttura UNICA e coerente)
# ============================================================================

@dataclass
class Lead:
    """
    FIX HTML COHERENCE: Struttura dati UNICA per ogni lead.
    Garantisce che tutti i campi appartengano alla STESSA auto.
    Usata sia per CSV che per HTML.
    """
    id: int = 0
    annuncio_id: str = ""
    url: str = ""
    piattaforma: str = ""
    titolo: str = ""
    marca: str = ""
    modello: str = ""
    anno: int = 0
    km: int = 0
    prezzo: int = 0
    comune: str = ""
    descrizione_raw: str = ""
    foto_base64: Optional[str] = None
    tipo_venditore: str = "privato"
    nome_venditore: str = ""
    telefono_contatto: str = ""
    giorni_online: Optional[int] = None
    storico_ribassi_info: Dict = field(default_factory=dict)
    stato_lead: str = "da_contattare"
    data_scraping: str = ""
    segmento: str = ""
    valore_mercato_stimato: Optional[int] = None
    prezzo_richiesto: int = 0
    scostamento_euro: Optional[int] = None
    scostamento_percentuale: Optional[float] = None
    prezzo_vendita_realistico: Optional[int] = None
    provvigione_stimata: Optional[int] = None
    priorita_conto_vendita: str = "media"
    note_conto_vendita: str = ""
    descrizione_annuncio: str = ""
    spunto_trattativa: str = ""
    business_score: int = 50

    def to_dict(self) -> Dict:
        """Converte in dizionario per compatibilità."""
        return asdict(self)

    def get_titolo_completo(self) -> str:
        """FIX HTML COHERENCE: Genera titolo coerente da campi interni."""
        return f"{self.marca} {self.modello}".strip()

# ============================================================================
# FIX QUERY GENERAL: Dataclass per Query (struttura tracciabile)
# ============================================================================

@dataclass
class QueryRicerca:
    """
    FIX QUERY GENERAL: Struttura per ogni query di ricerca.
    Traccia marca/modello per validazione risultati.
    """
    id: int
    query_text: str
    marca: str
    modello: str
    segmento: str
    portale: str = ""  # Vuoto = tutti i portali
    attiva: bool = True

    def __str__(self) -> str:
        stato = "✓" if self.attiva else "✗"
        return f"[{stato}] {self.id:3d}. {self.marca} {self.modello} ({self.segmento})"

    def descrizione_estesa(self) -> str:
        return f"Query: '{self.query_text}' | Marca: {self.marca} | Modello: {self.modello}"

# ============================================================================
# SEGMENTI E MARCHE
# ============================================================================

SEGMENTI_CONFIG = {
    'suv_premium': {
        'descrizione': 'SUV Premium e Lusso',
        'marche': {
            'BMW': ['X3', 'X5', 'X1', 'iX3'],
            'Audi': ['Q5', 'Q3', 'Q7', 'e-tron'],
            'Mercedes': ['GLC', 'GLE', 'GLA', 'GLB'],
            'Volvo': ['XC60', 'XC40', 'XC90'],
            'Porsche': ['Macan', 'Cayenne'],
            'Land Rover': ['Evoque', 'Velar', 'Discovery'],
            'Alfa Romeo': ['Stelvio'],
            'Lexus': ['NX', 'RX', 'UX'],
            'Jaguar': ['F-Pace', 'E-Pace'],
            'Tesla': ['Model X', 'Model Y'],
        }
    },
    'berlina_premium': {
        'descrizione': 'Berline Premium',
        'marche': {
            'BMW': ['Serie 3', 'Serie 5', 'i4'],
            'Audi': ['A4', 'A5', 'A6'],
            'Mercedes': ['Classe C', 'Classe E', 'CLA'],
            'Volvo': ['S60', 'V60'],
            'Alfa Romeo': ['Giulia'],
            'Tesla': ['Model 3', 'Model S'],
        }
    },
    'suv_medi': {
        'descrizione': 'SUV Segmento Medio',
        'marche': {
            'Hyundai': ['Tucson', 'Kona'],
            'Kia': ['Sportage', 'Niro'],
            'Toyota': ['RAV4', 'C-HR'],
            'Mazda': ['CX-5', 'CX-30'],
            'Ford': ['Kuga', 'Puma'],
            'Volkswagen': ['Tiguan', 'T-Roc'],
            'Peugeot': ['3008', '5008'],
            'Citroen': ['C5 Aircross'],
            'Jeep': ['Compass', 'Renegade'],
            'Nissan': ['Qashqai', 'X-Trail'],
        }
    },
    'compatte_premium': {
        'descrizione': 'Compatte Premium',
        'marche': {
            'Audi': ['A3', 'A1'],
            'BMW': ['Serie 1', 'Serie 2'],
            'Mercedes': ['Classe A', 'Classe B'],
            'Mini': ['Cooper', 'Countryman'],
            'Volkswagen': ['Golf', 'Polo', 'ID.3'],
            'Cupra': ['Formentor', 'Leon'],
            'Seat': ['Leon', 'Ateca'],
            'Skoda': ['Octavia', 'Kodiaq'],
        }
    },
}

def get_tutti_marchi() -> List[str]:
    marchi = set()
    for segmento in SEGMENTI_CONFIG.values():
        for marca in segmento['marche'].keys():
            marchi.add(marca)
    return sorted(list(marchi))

TUTTI_I_MARCHI = get_tutti_marchi()
MARCHE_PREMIUM = ['BMW', 'Audi', 'Mercedes', 'Volvo', 'Porsche', 'Land Rover', 'Maserati', 'Alfa Romeo', 'Lexus', 'Jaguar', 'Tesla', 'Mini']

# ============================================================================
# NORMALIZZAZIONE
# ============================================================================

MARCHE_NORMALIZE = {
    'bmw': 'BMW', 'audi': 'Audi', 'mercedes': 'Mercedes', 'mercedes-benz': 'Mercedes',
    'volvo': 'Volvo', 'porsche': 'Porsche', 'land rover': 'Land Rover', 'landrover': 'Land Rover',
    'alfa romeo': 'Alfa Romeo', 'alfaromeo': 'Alfa Romeo', 'alfa': 'Alfa Romeo',
    'lexus': 'Lexus', 'jaguar': 'Jaguar', 'tesla': 'Tesla', 'maserati': 'Maserati',
    'volkswagen': 'Volkswagen', 'vw': 'Volkswagen', 'ford': 'Ford', 'peugeot': 'Peugeot',
    'citroen': 'Citroen', 'citroën': 'Citroen', 'hyundai': 'Hyundai', 'kia': 'Kia',
    'toyota': 'Toyota', 'mazda': 'Mazda', 'nissan': 'Nissan', 'jeep': 'Jeep',
    'mini': 'Mini', 'cupra': 'Cupra', 'seat': 'Seat', 'skoda': 'Skoda', 'škoda': 'Skoda',
}

MODELLI_NORMALIZE = {
    'x3': 'X3', 'x5': 'X5', 'x1': 'X1', 'serie-3': 'Serie 3', 'serie-5': 'Serie 5',
    'serie3': 'Serie 3', 'serie5': 'Serie 5', 'serie1': 'Serie 1', 'serie2': 'Serie 2',
    'q5': 'Q5', 'q3': 'Q3', 'q7': 'Q7', 'a4': 'A4', 'a3': 'A3', 'a6': 'A6',
    'glc': 'GLC', 'gle': 'GLE', 'gla': 'GLA', 'classe-c': 'Classe C', 'classe-e': 'Classe E',
    'xc60': 'XC60', 'xc40': 'XC40', 'xc90': 'XC90', 'stelvio': 'Stelvio', 'giulia': 'Giulia',
    'tucson': 'Tucson', 'sportage': 'Sportage', 'rav4': 'RAV4', 'tiguan': 'Tiguan',
    'f-pace': 'F-Pace', 'fpace': 'F-Pace', 'e-pace': 'E-Pace', 'epace': 'E-Pace',
}

# ============================================================================
# VERIFICA DIPENDENZE
# ============================================================================

print("\n" + "=" * 70)
print("   BROKER AUTO AI v7.0.1 - QUERY CONTROL + COERENZA DATI")
print("=" * 70)
print(f"\n   Portali attivi: {', '.join(PORTALI_ATTIVI)}")
print(f"   Query default: {DEFAULT_MAX_QUERIES}")
print(f"   Debug mode: {DEBUG_MODE}")
print("\n   Verifica dipendenze...")

missing = []
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    print("   [OK] playwright")
except ImportError:
    print("   [X] playwright")
    missing.append("playwright")

try:
    import requests
    print("   [OK] requests")
except ImportError:
    print("   [X] requests")
    missing.append("requests")

try:
    from PIL import Image
    print("   [OK] pillow")
except ImportError:
    print("   [X] pillow")
    missing.append("pillow")

if missing:
    print(f"\n   INSTALLA: pip install {' '.join(missing)}")
    print("   POI: python -m playwright install chromium\n")
    sys.exit(1)

print("   Dipendenze OK!\n")

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
import requests
from PIL import Image

# ============================================================================
# LOGGING
# ============================================================================

log_level = logging.DEBUG if DEBUG_MODE else logging.INFO
logging.basicConfig(
    level=log_level,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(CONFIG_OUTPUT_DIR, 'broker_auto.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# SELETTORI CSS E URL
# ============================================================================

SELETTORI = {
    'autoscout24': {
        'search_url': 'https://www.autoscout24.it/lst?sort=standard&desc=0&cy=I&atype=C&ustate=N%2CU&search_id=1&query={query}',
        'wait_selector': 'article[data-testid], [class*="ListItem"], main',
        'ad_links': [
            'article[data-testid="listing"] a[href*="/annunci/"]',
            'a[href*="/annunci/"][href*="-id"]',
            'main article a[href*="/annunci/"]',
        ],
        'title': ['h1', '[data-testid="listing-title"]', '[class*="title"]'],
        'price': ['[data-testid="price"]', '[class*="Price"]', 'span[class*="price"]'],
        'location': ['[data-testid="location"]', '[class*="location"]'],
        'description': ['[data-testid="description"]', '[class*="description"]'],
        'images': ['img[src*="autoscout24"]', 'picture img'],
        'seller_type': ['[data-testid="seller-type"]', '[class*="seller"]'],
        'seller_name': ['[data-testid="seller-name"]', '[class*="SellerInfo"]'],
        'phone': ['a[href^="tel:"]'],
        'base_url': 'https://www.autoscout24.it',
    },
    'automobile': {
        'search_url': 'https://www.automobile.it/annunci-auto?q={query}',
        'wait_selector': '[class*="listing"], [class*="card"], article, main',
        'ad_links': [
            'a[href*="/annuncio-auto/"]',
            'a[href*="/auto-usate/"]',
            '[class*="listing"] a[href*="annuncio"]',
        ],
        'title': ['h1', '[class*="title"]'],
        'price': ['[class*="price"]', '[class*="Price"]'],
        'location': ['[class*="location"]', '[class*="city"]'],
        'description': ['[class*="description"]'],
        'images': ['img[src*="automobile.it"]', 'picture img'],
        'seller_type': ['[class*="seller"]', '[class*="badge"]'],
        'seller_name': ['[class*="name"]', '[class*="seller"]'],
        'phone': ['a[href^="tel:"]'],
        'base_url': 'https://www.automobile.it',
    },
}

# ============================================================================
# UTILITY
# ============================================================================

def sanitize_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    text = text.strip()
    if len(text) > 500:
        text = text[:500]
    text = re.sub(r'\s+', ' ', text).strip()
    return ''.join(c for c in text if c.isprintable() or c in '\n\t').strip()

def normalizza_prezzo(raw: str) -> Tuple[int, bool]:
    if not raw or not isinstance(raw, str):
        return 0, True
    text = raw.strip()
    text = re.sub(r'[€$\s]', '', text).replace('EUR', '').replace('euro', '')

    prezzo = 0
    patterns = [
        (r'^(\d{1,3})\.(\d{3}),\d{2}$', lambda m: int(m.group(1) + m.group(2))),
        (r'^(\d{1,3})\.(\d{3})$', lambda m: int(m.group(1) + m.group(2))),
        (r'^(\d{4,6})$', lambda m: int(m.group(1))),
    ]

    for pattern, converter in patterns:
        m = re.match(pattern, text)
        if m:
            prezzo = converter(m)
            break

    if prezzo == 0:
        digits = re.sub(r'[^\d]', '', text)
        if digits:
            try:
                prezzo = int(digits)
            except:
                return 0, True

    is_sospetto = prezzo < 3000 or prezzo > 300000
    return prezzo, is_sospetto

def formatta_prezzo_euro(p: int) -> str:
    if p <= 0:
        return "N/A"
    return f"€ {p:,}".replace(',', '.')

# ============================================================================
# FIX QUERY GENERAL: Validatore Coerenza Query/Risultati
# ============================================================================

class ValidatoreCoerenzaQuery:
    """
    FIX QUERY GENERAL: Verifica che i risultati corrispondano alla query.
    Scarta annunci di marche/modelli diversi da quelli cercati.
    """

    # Marche note per esclusione incrociata
    TUTTE_MARCHE = list(MARCHE_NORMALIZE.values())

    @classmethod
    def normalizza(cls, testo: str) -> str:
        if not testo:
            return ""
        return re.sub(r'[^a-z0-9]', '', testo.lower())

    @classmethod
    def url_contiene_marca(cls, url: str, marca_cercata: str) -> bool:
        """Verifica se l'URL contiene la marca cercata."""
        url_lower = url.lower()
        marca_lower = marca_cercata.lower()

        # Match diretto o con trattini
        varianti = [marca_lower, marca_lower.replace(' ', '-'), marca_lower.replace(' ', '')]
        return any(v in url_lower for v in varianti)

    @classmethod
    def url_contiene_altra_marca(cls, url: str, marca_cercata: str) -> Tuple[bool, str]:
        """Verifica se l'URL contiene una marca DIVERSA da quella cercata."""
        url_lower = url.lower()
        marca_cercata_lower = marca_cercata.lower()

        for marca in cls.TUTTE_MARCHE:
            marca_lower = marca.lower()
            if marca_lower != marca_cercata_lower:
                varianti = [marca_lower, marca_lower.replace(' ', '-'), marca_lower.replace(' ', '')]
                for v in varianti:
                    if v in url_lower and len(v) > 2:  # Evita match troppo corti
                        return True, marca

        return False, ""

    @classmethod
    def risultato_coerente_con_query(cls, url: str, titolo: str, query: QueryRicerca) -> Tuple[bool, str]:
        """
        FIX QUERY GENERAL: Verifica coerenza risultato con query.
        Restituisce (True, "") se coerente, (False, motivo) altrimenti.
        """
        marca = query.marca

        # 1. URL deve contenere la marca cercata
        if not cls.url_contiene_marca(url, marca):
            return False, f"URL non contiene '{marca}'"

        # 2. URL non deve contenere altre marche
        ha_altra, altra_marca = cls.url_contiene_altra_marca(url, marca)
        if ha_altra:
            return False, f"URL contiene marca diversa: '{altra_marca}'"

        # 3. Titolo (se disponibile) deve contenere la marca
        if titolo:
            titolo_lower = titolo.lower()
            marca_lower = marca.lower()
            if marca_lower not in titolo_lower:
                # Verifica varianti
                varianti = [marca_lower, marca_lower.replace(' ', '')]
                if not any(v in titolo_lower for v in varianti):
                    # FIX: Non è bloccante qui, ma logghiamo per debug
                    logger.debug(f"   [WARN] Titolo non contiene marca cercata: '{titolo[:50]}' vs '{marca}'")

        return True, "OK"

    @classmethod
    def valida_titolo_post_scrape(cls, titolo: str, query: QueryRicerca) -> Tuple[bool, str]:
        """
        FIX: Validazione CRITICA del titolo dopo lo scrape.
        Verifica che il titolo non contenga una marca DIVERSA da quella cercata.
        Questo evita di salvare un lead "BMW X3" quando l'annuncio è in realtà "Audi Q5".
        """
        if not titolo:
            return True, "OK (nessun titolo)"

        titolo_lower = titolo.lower()
        marca_cercata = query.marca.lower()

        # Verifica se il titolo contiene un'altra marca
        for marca_norm in cls.TUTTE_MARCHE:
            marca_check = marca_norm.lower()
            if marca_check == marca_cercata:
                continue  # Skip la marca che stiamo cercando

            # Varianti della marca
            varianti = [marca_check, marca_check.replace(' ', '-'), marca_check.replace(' ', '')]
            for v in varianti:
                if len(v) > 2 and v in titolo_lower:
                    # Trovata altra marca nel titolo!
                    return False, f"Titolo contiene marca diversa: '{marca_norm}' (cercavamo '{query.marca}')"

        # Verifica che il titolo contenga la marca cercata
        varianti_cercata = [marca_cercata, marca_cercata.replace(' ', '-'), marca_cercata.replace(' ', '')]
        if not any(v in titolo_lower for v in varianti_cercata):
            # Il titolo non contiene la marca cercata - potrebbe essere un problema
            logger.warning(f"   [WARN] Titolo '{titolo[:40]}' non contiene '{query.marca}'")
            # Non blocchiamo, ma è un warning importante

        return True, "OK"

# ============================================================================
# FILTRI
# ============================================================================

class FiltroRivenditori:
    """Filtro per escludere concessionari - SOLO PRIVATI."""

    KEYWORDS_NOME = [
        'auto', 'motor', 'motors', 'car', 'cars', 'garage', 'concession', 'dealer',
        'rivenditor', 'salone', 'automarket', 'autoservice', 'rent', 'rental',
        'srl', 'snc', 'spa', 'sas', 'group', 'trading', 'store', 'center',
    ]

    ETICHETTE_DEALER = [
        'concessionario', 'dealer', 'professionista', 'azienda', 'rivenditore',
        'professional', 'commerciante', 'pro', 'business',
    ]

    @classmethod
    def is_rivenditore(cls, nome_venditore: str, descrizione: str,
                       etichetta_portale: str, page_content: str) -> Tuple[bool, str]:
        score = 0
        nome_lower = (nome_venditore or '').lower().strip()
        etichetta_lower = (etichetta_portale or '').lower()

        for e in cls.ETICHETTE_DEALER:
            if e in etichetta_lower:
                return True, f"Etichetta: {e}"

        for kw in cls.KEYWORDS_NOME:
            if kw.lower() in nome_lower:
                score += 35
                if score >= 50:
                    break

        if re.search(r'\b(s\.?r\.?l\.?|s\.?n\.?c\.?|s\.?p\.?a\.?)\b', nome_lower):
            return True, "Forma societaria"

        return score >= 50, f"Score {score}" if score >= 50 else ""

class FiltroPrezziFalsi:
    """
    FIX: Filtro esteso per prezzi promozionali/finanziamenti.
    Aggiunge molte più keywords per identificare prezzi non reali.
    """
    # FIX: Lista estesa di keywords per identificare prezzi promozionali
    KEYWORDS = [
        'promo finanziamento', 'solo con finanziamento', 'rate da', 'leasing',
        'rata mensile', 'prezzo con finanziamento', 'finanziamento incluso',
        'a partire da', 'prezzo promo', 'offerta speciale', 'prezzo scontato',
        'tan ', 'taeg ', 'tasso zero', 'zero interessi', 'prima rata',
        'anticipo zero', 'senza anticipo', 'maxirata', 'valore futuro garantito',
        'noleggio lungo termine', 'nlt ', 'pay per drive', 'prezzo chiavi in mano',
        'supervalutazione usato', 'rottamazione', 'incentivo statale',
        'ecoincentivo', 'contributo rottamazione', 'prezzo lancio',
    ]

    # FIX: Pattern regex per prezzi sospetti (es: "da 199€/mese")
    PATTERN_RATE = re.compile(r'\b\d{2,3}\s*€?\s*/?\s*mes[ei]', re.IGNORECASE)
    PATTERN_DA_PREZZO = re.compile(r'\bda\s+€?\s*\d', re.IGNORECASE)

    @classmethod
    def is_prezzo_promo(cls, titolo: str, descrizione: str, prezzo_raw: str = "") -> Tuple[bool, str]:
        """
        FIX: Verifica se il prezzo è promozionale/finanziamento.
        Aggiunto parametro prezzo_raw per controllare anche il testo del prezzo.
        """
        combined = f"{titolo} {descrizione} {prezzo_raw}".lower()

        # Check keywords
        matches = [kw for kw in cls.KEYWORDS if kw in combined]
        if matches:
            return True, f"Promo keyword: {matches[0]}"

        # FIX: Check pattern rate mensili
        if cls.PATTERN_RATE.search(combined):
            return True, "Promo: prezzo mensile rilevato"

        # FIX: Check pattern "da X euro"
        if cls.PATTERN_DA_PREZZO.search(combined):
            return True, "Promo: prezzo 'a partire da'"

        return False, ""

# ============================================================================
# FIX QUERY CONTROL: Generatore Query con Controllo Utente
# ============================================================================

class GeneratoreQueryConControllo:
    """
    FIX QUERY CONTROL: Genera query con possibilità di controllo utente.
    - L'utente può scegliere quante query
    - L'utente può rivedere e modificare le query
    """

    @classmethod
    def genera_query_da_marchi(cls, marchi_selezionati: List[str]) -> List[QueryRicerca]:
        """
        FIX QUERY GENERAL: Genera query allineate alle carline.
        Ogni query ha marca e modello tracciati per validazione.
        """
        query_list = []
        query_id = 1

        for segmento_name, segmento_config in SEGMENTI_CONFIG.items():
            for marca, modelli in segmento_config['marche'].items():
                if marca not in marchi_selezionati:
                    continue
                for modello in modelli:
                    # FIX QUERY GENERAL: Query costruita con marca+modello espliciti
                    query = QueryRicerca(
                        id=query_id,
                        query_text=f"{marca} {modello}",
                        marca=marca,
                        modello=modello,
                        segmento=segmento_name,
                        attiva=True
                    )
                    query_list.append(query)
                    query_id += 1

        random.shuffle(query_list)

        # Rinumera dopo shuffle
        for i, q in enumerate(query_list, 1):
            q.id = i

        return query_list

    @classmethod
    def mostra_riepilogo_query(cls, queries: List[QueryRicerca]):
        """FIX QUERY CONTROL: Mostra riepilogo query generate."""
        print(f"\n   📋 QUERY GENERATE: {len(queries)}")
        print("   " + "-" * 50)

        # Conta per marca
        per_marca = {}
        for q in queries:
            per_marca[q.marca] = per_marca.get(q.marca, 0) + 1

        for marca, count in sorted(per_marca.items()):
            print(f"      {marca}: {count} modelli")

        print("   " + "-" * 50)

    @classmethod
    def chiedi_numero_query(cls, queries: List[QueryRicerca]) -> int:
        """
        FIX QUERY CONTROL: Chiede all'utente quante query lanciare.
        """
        max_disponibili = len(queries)
        default = min(DEFAULT_MAX_QUERIES, max_disponibili)

        print(f"\n   Quante query vuoi lanciare?")
        print(f"   (disponibili: {max_disponibili}, default: {default})")

        while True:
            try:
                risposta = input(f"   Numero query [{default}]: ").strip()

                if not risposta:
                    return default

                n = int(risposta)
                if 1 <= n <= max_disponibili:
                    return n
                else:
                    print(f"   ⚠️  Inserisci un numero tra 1 e {max_disponibili}")
            except ValueError:
                print("   ⚠️  Inserisci un numero valido")

    @classmethod
    def review_and_edit_queries(cls, queries: List[QueryRicerca]) -> List[QueryRicerca]:
        """
        FIX QUERY REVIEW: Permette all'utente di rivedere e modificare le query.
        """
        print(f"\n   Vuoi rivedere l'elenco delle {len(queries)} query? (s/n)")
        risposta = input("   [n]: ").strip().lower()

        if risposta != 's':
            return [q for q in queries if q.attiva]

        # Mostra elenco
        print("\n   " + "=" * 60)
        print("   ELENCO QUERY")
        print("   " + "=" * 60)

        for q in queries:
            stato = "✓" if q.attiva else "✗"
            print(f"   [{stato}] {q.id:3d}. {q.marca:<15} {q.modello:<15} ({q.segmento})")

        print("\n   " + "-" * 60)
        print("   OPZIONI:")
        print("   - Inserisci numeri da ESCLUDERE (es: 2,5,7)")
        print("   - '+' seguito da numeri per RIATTIVARE (es: +2,5,7)")  # FIX: Aggiunto comando riattivazione
        print("   - 'm' seguito da numero per MODIFICARE (es: m3)")
        print("   - 'ok' per confermare e procedere")
        print("   - 'all' per includere tutte le query")
        print("   - 'list' per rivedere l'elenco")  # FIX: Aggiunto comando list
        print("   " + "-" * 60)

        while True:
            cmd = input("\n   Comando: ").strip().lower()

            if cmd == 'ok' or cmd == '':
                break

            if cmd == 'all':
                for q in queries:
                    q.attiva = True
                print("   ✓ Tutte le query attivate")
                continue

            # FIX: Aggiunto comando 'list' per rivedere l'elenco
            if cmd == 'list':
                print("\n   " + "=" * 60)
                print("   ELENCO QUERY AGGIORNATO")
                print("   " + "=" * 60)
                for q in queries:
                    stato = "✓" if q.attiva else "✗"
                    print(f"   [{stato}] {q.id:3d}. {q.marca:<15} {q.modello:<15} ({q.segmento})")
                attive_count = len([q for q in queries if q.attiva])
                print(f"\n   Query attive: {attive_count}/{len(queries)}")
                continue

            # FIX: Aggiunto comando '+' per riattivare query singole
            if cmd.startswith('+'):
                try:
                    indici = [int(x.strip()) for x in cmd[1:].split(',') if x.strip()]
                    for idx in indici:
                        query_da_riatt = next((q for q in queries if q.id == idx), None)
                        if query_da_riatt:
                            query_da_riatt.attiva = True
                            print(f"   ✓ Riattivata: {query_da_riatt.marca} {query_da_riatt.modello}")
                        else:
                            print(f"   ⚠️ Query {idx} non trovata")
                except:
                    print("   ⚠️ Formato: +2,5,7")
                continue

            # Modifica query
            if cmd.startswith('m'):
                try:
                    idx = int(cmd[1:])
                    query_da_mod = next((q for q in queries if q.id == idx), None)
                    if query_da_mod:
                        print(f"\n   Modifica query {idx}: {query_da_mod.query_text}")
                        nuovo_testo = input("   Nuovo testo query (ENTER per annullare): ").strip()
                        if nuovo_testo:
                            query_da_mod.query_text = nuovo_testo
                            print(f"   ✓ Query modificata: {nuovo_testo}")
                    else:
                        print(f"   ⚠️ Query {idx} non trovata")
                except:
                    print("   ⚠️ Formato: m<numero> (es: m3)")
                continue

            # Escludi query
            try:
                indici = [int(x.strip()) for x in cmd.split(',') if x.strip()]
                for idx in indici:
                    query_da_escl = next((q for q in queries if q.id == idx), None)
                    if query_da_escl:
                        query_da_escl.attiva = False
                        print(f"   ✗ Esclusa: {query_da_escl.marca} {query_da_escl.modello}")
            except:
                print("   ⚠️ Formato non valido. Usa: 2,5,7 oppure m3 oppure ok")

        # Restituisci solo query attive
        attive = [q for q in queries if q.attiva]
        print(f"\n   ✓ Query finali: {len(attive)}")
        return attive

    @classmethod
    def conta_query_per_marchio(cls, marca: str) -> int:
        count = 0
        for config in SEGMENTI_CONFIG.values():
            if marca in config['marche']:
                count += len(config['marche'][marca])
        return count

    @classmethod
    def riepilogo_marchi(cls, marchi: List[str]) -> Dict:
        riepilogo = {'totale': 0, 'per_marchio': {}}
        for marca in marchi:
            n = cls.conta_query_per_marchio(marca)
            riepilogo['per_marchio'][marca] = n
            riepilogo['totale'] += n
        return riepilogo

# ============================================================================
# VALUTATORE MERCATO
# ============================================================================

class ValutatoreMercato:
    def __init__(self):
        self.database: List[Lead] = []

    def aggiungi(self, lead: Lead):
        if lead.prezzo > 0:
            self.database.append(lead)

    def calcola(self, lead: Lead) -> Dict[str, Any]:
        result = {
            'valore_mercato_stimato': None, 'prezzo_richiesto': lead.prezzo,
            'scostamento_euro': None, 'scostamento_percentuale': None,
            'prezzo_vendita_realistico': None, 'provvigione_stimata': None,
            'priorita_conto_vendita': 'media', 'note_conto_vendita': '',
        }

        if lead.prezzo <= 0:
            return result

        BASELINE = {'suv_premium': 42000, 'berlina_premium': 35000, 'suv_medi': 25000, 'compatte_premium': 22000, 'altro': 18000}
        base = BASELINE.get(lead.segmento, 18000)
        adj = base * (1 - 0.08 * max(0, 2024 - lead.anno))
        valore = max(8000, int(adj))

        result['valore_mercato_stimato'] = valore
        result['scostamento_euro'] = lead.prezzo - valore
        result['scostamento_percentuale'] = round((result['scostamento_euro'] / valore) * 100, 1) if valore > 0 else 0

        scost = result['scostamento_percentuale']
        result['prezzo_vendita_realistico'] = int(valore * 0.97)
        result['provvigione_stimata'] = int(result['prezzo_vendita_realistico'] * CONFIG_PROVVIGIONE_CONTO_VENDITA)

        if scost <= 10:
            result['priorita_conto_vendita'] = 'alta'
        elif scost <= 15:
            result['priorita_conto_vendita'] = 'media'
        else:
            result['priorita_conto_vendita'] = 'bassa'

        return result

# ============================================================================
# SCRAPER MULTI-PORTALE v7.0
# ============================================================================

class BrokerScraperV7:
    """
    Scraper v7.0 con controllo query e coerenza dati.

    FIX QUERY CONTROL: Usa QueryRicerca per tracciare marca/modello
    FIX HTML COHERENCE: Usa dataclass Lead per struttura unica
    """

    def __init__(self, queries_approvate: List[QueryRicerca]):
        self.queries = queries_approvate
        self.leads: List[Lead] = []  # FIX HTML COHERENCE: Lista di Lead tipizzati
        self.stats = {
            'totali': 0,
            'dealer_esclusi': 0,
            'promo_esclusi': 0,
            'errori': 0,
            'pagine_errore': 0,
            'non_coerenti': 0,  # FIX QUERY GENERAL: Contatore risultati scartati
            'per_portale': {p: 0 for p in PORTALI_ATTIVI},
            'links_trovati': {p: 0 for p in PORTALI_ATTIVI},
        }
        self.urls_visti: set = set()
        self.valutatore = ValutatoreMercato()
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        ]

    async def init_browser(self):
        logger.info("Avvio browser...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled']
        )
        await self._new_context()
        logger.info("Browser pronto")

    async def _new_context(self):
        if self.context:
            try: await self.context.close()
            except: pass

        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent=random.choice(self.user_agents),
            locale='it-IT',
            timezone_id='Europe/Rome',
        )

        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        self.page = await self.context.new_page()

    async def _extract(self, sels: List[str], default: str = "") -> str:
        for sel in sels:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0:
                    text = await el.text_content(timeout=CONFIG_TIMEOUT_ELEMENTO)
                    if text:
                        clean = sanitize_text(text)
                        if clean:
                            return clean
            except:
                pass
        return default

    async def _chiudi_popup(self):
        popup_sels = [
            'button[id*="accept"]', 'button[class*="accept"]', 'button[class*="consent"]',
            '#onetrust-accept-btn-handler', '[data-testid*="accept"]',
        ]
        for sel in popup_sels:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=2000)
                    await asyncio.sleep(0.5)
            except:
                pass

    async def cerca_annunci_portale(self, query: QueryRicerca, portale: str) -> List[str]:
        """
        FIX QUERY GENERAL: Cerca con validazione coerenza risultati.
        """
        if portale not in SELETTORI:
            return []

        config = SELETTORI[portale]
        query_encoded = quote_plus(query.query_text)
        search_url = config['search_url'].format(query=query_encoded)

        try:
            logger.info(f"   [{portale.upper()}] Query: {query.query_text}")

            await self.page.goto(search_url, wait_until='domcontentloaded', timeout=CONFIG_TIMEOUT_NAVIGAZIONE)

            wait_sel = config.get('wait_selector', 'article, main')
            try:
                await self.page.wait_for_selector(wait_sel, timeout=12000, state='attached')
            except:
                pass

            await asyncio.sleep(random.uniform(2, 4))
            await self._chiudi_popup()

            # Scroll per lazy loading
            for _ in range(3):
                await self.page.evaluate('window.scrollBy(0, window.innerHeight / 2)')
                await asyncio.sleep(0.5)

            urls_valide = []
            base_url = config.get('base_url', '')

            for sel in config.get('ad_links', []):
                try:
                    elements = await self.page.locator(sel).all()

                    for link in elements:
                        try:
                            href = await link.get_attribute('href')
                            if not href:
                                continue

                            if href.startswith('/'):
                                href = base_url + href

                            if not href.startswith('http') or href in self.urls_visti:
                                continue

                            # Escludi pagine sistema
                            exclude = ['/lst', '/search', '?q=', '/ricerca', 'cookie', 'privacy', 'login']
                            if any(x in href.lower() for x in exclude):
                                continue

                            # FIX QUERY GENERAL: Validazione coerenza con query
                            is_coerente, motivo = ValidatoreCoerenzaQuery.risultato_coerente_con_query(
                                href, "", query
                            )

                            if not is_coerente:
                                logger.debug(f"   [SKIP] {href[:50]}... - {motivo}")
                                self.stats['non_coerenti'] += 1
                                continue

                            urls_valide.append(href)

                            if len(urls_valide) >= MAX_LEAD_PER_QUERY:
                                break

                        except:
                            continue

                    if len(urls_valide) >= MAX_LEAD_PER_QUERY:
                        break

                except:
                    continue

            urls_uniche = list(dict.fromkeys(urls_valide))[:MAX_LEAD_PER_QUERY]
            self.stats['links_trovati'][portale] += len(urls_uniche)
            logger.info(f"   [{portale.upper()}] Trovati: {len(urls_uniche)} annunci coerenti")

            return urls_uniche

        except Exception as e:
            logger.warning(f"   [{portale.upper()}] Errore: {str(e)[:50]}")
            self.stats['errori'] += 1
            return []

    async def scrape_annuncio(self, url: str, portale: str, query: QueryRicerca) -> Optional[Lead]:
        """
        FIX HTML COHERENCE: Scrape singolo annuncio restituendo Lead strutturato.
        """
        if url in self.urls_visti:
            return None
        self.urls_visti.add(url)

        config = SELETTORI.get(portale, {})
        annuncio_id = hashlib.md5(url.encode()).hexdigest()[:12]

        for attempt in range(CONFIG_MAX_RETRY):
            try:
                logger.info(f"   [{portale.upper()}] {url[:60]}...")
                await self.page.goto(url, wait_until='domcontentloaded', timeout=CONFIG_TIMEOUT_NAVIGAZIONE)
                await asyncio.sleep(random.uniform(2, 3))
                await self._chiudi_popup()

                title = await self._extract(config.get('title', []))
                price_text = await self._extract(config.get('price', []))
                location = await self._extract(config.get('location', []))
                description = await self._extract(config.get('description', []))
                seller_name = await self._extract(config.get('seller_name', []))
                seller_type = await self._extract(config.get('seller_type', []))

                # FIX: Estrazione telefono (era dichiarato nei selettori ma non estratto)
                telefono = ""
                for phone_sel in config.get('phone', []):
                    try:
                        phone_el = self.page.locator(phone_sel).first
                        if await phone_el.count() > 0:
                            href = await phone_el.get_attribute('href')
                            if href and href.startswith('tel:'):
                                telefono = href.replace('tel:', '').strip()
                                break
                    except:
                        pass

                page_content = await self.page.content()

                # FIX CRITICO: Validazione titolo post-scrape per evitare marche mescolate
                is_titolo_ok, motivo_titolo = ValidatoreCoerenzaQuery.valida_titolo_post_scrape(title, query)
                if not is_titolo_ok:
                    logger.info(f"   [MARCA ERRATA] {motivo_titolo}")
                    self.stats['non_coerenti'] += 1
                    return None

                # Filtro rivenditori
                is_dealer, motivo = FiltroRivenditori.is_rivenditore(seller_name, description, seller_type, page_content)
                if is_dealer:
                    logger.info(f"   [DEALER] {motivo}")
                    self.stats['dealer_esclusi'] += 1
                    return None

                # FIX: Filtro promo con prezzo_raw per maggiore accuratezza
                is_promo, motivo_promo = FiltroPrezziFalsi.is_prezzo_promo(title, description, price_text)
                if is_promo:
                    logger.info(f"   [PROMO] {motivo_promo}")
                    self.stats['promo_esclusi'] += 1
                    return None

                # Prezzo
                prezzo, _ = normalizza_prezzo(price_text)
                if prezzo < CONFIG_PREZZO_MIN or prezzo > CONFIG_PREZZO_MAX:
                    return None

                # Anno/KM
                combined = f"{title} {description}"
                anno_match = re.search(r'\b(20[12][0-9])\b', combined)
                anno = int(anno_match.group(1)) if anno_match else 2020

                km = 70000
                for p in [r'(\d{1,3})\.(\d{3})\s*km', r'(\d{4,6})\s*km']:
                    m = re.search(p, combined.lower())
                    if m:
                        km_str = ''.join(str(g) for g in m.groups() if g).replace('.', '')
                        try:
                            km = int(km_str)
                            if km > 500000: km = 70000
                            break
                        except:
                            pass

                if anno < CONFIG_ANNO_MIN or km > CONFIG_KM_MAX:
                    return None

                # FIX HTML COHERENCE: Crea Lead strutturato con TUTTI i dati coerenti
                lead = Lead(
                    id=len(self.leads) + 1,
                    annuncio_id=annuncio_id,
                    url=url,
                    piattaforma=portale.upper(),
                    titolo=title[:150] if title else f"{query.marca} {query.modello}",
                    marca=query.marca,  # FIX: Usa marca dalla query
                    modello=query.modello,  # FIX: Usa modello dalla query
                    anno=anno,
                    km=km,
                    prezzo=prezzo,
                    prezzo_richiesto=prezzo,  # FIX: Inizializza prezzo_richiesto (era sempre 0)
                    comune=location.split()[0] if location else "",
                    descrizione_raw=description[:400] if description else "",
                    tipo_venditore='privato',
                    nome_venditore=seller_name[:80] if seller_name else '',
                    telefono_contatto=telefono,  # FIX: Aggiunto telefono estratto
                    data_scraping=datetime.now().isoformat(),
                    segmento=query.segmento,
                )

                self.stats['totali'] += 1
                self.stats['per_portale'][portale] += 1

                logger.info(f"   [OK] {lead.marca} {lead.modello} - {formatta_prezzo_euro(prezzo)}")
                return lead

            except Exception as e:
                if attempt < CONFIG_MAX_RETRY - 1:
                    await asyncio.sleep(2)
                else:
                    self.stats['errori'] += 1

        return None

    def post_elaborazione(self):
        """Post-elaborazione lead."""
        for lead in self.leads:
            self.valutatore.aggiungi(lead)

        for lead in self.leads:
            vm = self.valutatore.calcola(lead)
            lead.valore_mercato_stimato = vm.get('valore_mercato_stimato')
            lead.scostamento_euro = vm.get('scostamento_euro')
            lead.scostamento_percentuale = vm.get('scostamento_percentuale')
            lead.prezzo_vendita_realistico = vm.get('prezzo_vendita_realistico')
            lead.provvigione_stimata = vm.get('provvigione_stimata')
            lead.priorita_conto_vendita = vm.get('priorita_conto_vendita', 'media')

            lead.descrizione_annuncio = f"{lead.marca} {lead.modello} del {lead.anno}, {lead.km:,} km, richiesta {formatta_prezzo_euro(lead.prezzo)}."

            # Business score
            score = 50
            if lead.priorita_conto_vendita == 'alta': score += 30
            elif lead.priorita_conto_vendita == 'media': score += 15
            if lead.telefono_contatto: score += 15
            lead.business_score = min(100, score)

        # Ordina per score
        self.leads.sort(key=lambda x: x.business_score, reverse=True)

    async def esegui(self):
        """Esegue scraping con query controllate."""
        print("\n" + "=" * 70)
        print(f"   ESECUZIONE v7.0")
        print("=" * 70)
        print(f"   Query da elaborare: {len(self.queries)}")
        print(f"   Portali: {', '.join(PORTALI_ATTIVI)}")
        print("=" * 70 + "\n")

        await self.init_browser()

        try:
            for idx, query in enumerate(self.queries, 1):
                if len(self.leads) >= CONFIG_MAX_LEAD_TOTALI:
                    logger.info(f"Raggiunto limite {CONFIG_MAX_LEAD_TOTALI} lead. Stop.")
                    break

                logger.info(f"\n--- Query {idx}/{len(self.queries)}: {query.marca} {query.modello} ---")

                for portale in PORTALI_ATTIVI:
                    urls = await self.cerca_annunci_portale(query, portale)

                    for url in urls:
                        if len(self.leads) >= CONFIG_MAX_LEAD_TOTALI:
                            break

                        lead = await self.scrape_annuncio(url, portale, query)
                        if lead:
                            self.leads.append(lead)
                        await asyncio.sleep(random.uniform(CONFIG_DELAY_MIN, CONFIG_DELAY_MAX))

                    await asyncio.sleep(CONFIG_DELAY_TRA_PIATTAFORME)

                await asyncio.sleep(CONFIG_DELAY_TRA_QUERY)

                if idx % 10 == 0:
                    logger.info("Refresh browser...")
                    await self._new_context()

            self.post_elaborazione()

        finally:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()

    def salva_json(self):
        """FIX HTML COHERENCE: Salva usando struttura Lead."""
        filepath = os.path.join(CONFIG_OUTPUT_DIR, 'leads_broker.json')
        data = [lead.to_dict() for lead in self.leads]
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"   [OK] {filepath} ({len(self.leads)} lead)")

    def salva_csv(self):
        """FIX HTML COHERENCE: CSV da struttura Lead unificata."""
        filepath = os.path.join(CONFIG_OUTPUT_DIR, 'leads_broker.csv')
        # FIX: Aggiunto titolo e prezzo_richiesto per maggiore trasparenza
        campi = ['id', 'piattaforma', 'marca', 'modello', 'titolo', 'anno', 'km',
                 'prezzo', 'prezzo_richiesto', 'valore_mercato_stimato',
                 'scostamento_percentuale', 'priorita_conto_vendita', 'business_score',
                 'telefono_contatto', 'comune', 'tipo_venditore', 'segmento', 'url']

        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=campi, extrasaction='ignore')
            writer.writeheader()
            for lead in self.leads:
                writer.writerow(lead.to_dict())

        # FIX: Mostra statistiche aggiuntive
        con_tel = len([l for l in self.leads if l.telefono_contatto])
        print(f"   [OK] {filepath} ({len(self.leads)} lead, {con_tel} con telefono)")

    def genera_dashboard(self):
        """
        FIX HTML COHERENCE: Dashboard con dati coerenti.
        Ogni card usa lo STESSO oggetto Lead per tutti i campi.
        """
        total = len(self.leads)
        alta_prio = len([l for l in self.leads if l.priorita_conto_vendita == 'alta'])
        media_prio = len([l for l in self.leads if l.priorita_conto_vendita == 'media'])
        con_tel = len([l for l in self.leads if l.telefono_contatto])

        stats_portale = self.stats.get('per_portale', {})

        html = f'''<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Broker Auto v7.0 - {total} Lead</title>
<style>
:root {{ --primary: #e94560; --bg: #1a1a2e; --card: rgba(255,255,255,0.05); --text: #e8e8e8; --muted: #64748b; --success: #10b981; --warning: #f59e0b; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); min-height: 100vh; padding: 15px; color: var(--text); }}
.container {{ max-width: 1400px; margin: 0 auto; }}
.header {{ background: linear-gradient(135deg, #0f3460 0%, #16213e 100%); border-radius: 12px; padding: 20px; text-align: center; margin-bottom: 20px; }}
.header h1 {{ font-size: 1.6rem; background: linear-gradient(135deg, #e94560, #f39c12); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 10px; margin-bottom: 20px; }}
.stat {{ background: var(--card); padding: 12px; border-radius: 10px; text-align: center; }}
.stat-value {{ font-size: 1.4rem; font-weight: 800; color: var(--primary); }}
.stat-label {{ font-size: 0.7rem; color: var(--muted); text-transform: uppercase; }}
.leads {{ display: grid; gap: 15px; }}
.lead {{ background: var(--card); border-radius: 12px; overflow: hidden; transition: transform 0.2s; }}
.lead:hover {{ transform: translateY(-2px); }}
.lead-header {{ display: flex; justify-content: space-between; align-items: center; padding: 15px; border-bottom: 1px solid rgba(255,255,255,0.05); }}
.lead-title {{ font-weight: 600; font-size: 1rem; }}
.badge {{ padding: 5px 10px; border-radius: 5px; font-size: 0.65rem; font-weight: 700; text-transform: uppercase; }}
.badge-autoscout24 {{ background: #ff8c00; color: white; }}
.badge-automobile {{ background: #00a67e; color: white; }}
.lead-meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(80px, 1fr)); gap: 10px; padding: 15px; }}
.meta-item {{ text-align: center; }}
.meta-label {{ font-size: 0.65rem; color: var(--muted); display: block; margin-bottom: 3px; }}
.meta-value {{ font-size: 0.9rem; font-weight: 600; }}
.prio-alta {{ color: var(--success); }}
.prio-media {{ color: var(--warning); }}
.prio-bassa {{ color: var(--muted); }}
.btn-open {{ display: block; text-align: center; padding: 12px; background: var(--primary); color: white; text-decoration: none; font-weight: 600; transition: background 0.2s; }}
.btn-open:hover {{ background: #d63850; }}
.footer {{ text-align: center; padding: 25px; color: var(--muted); font-size: 0.75rem; }}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>🚗 Broker Auto v7.0.1 - Query Control</h1>
<p style="color: var(--muted); margin-top: 8px;">Multi-portale | Solo Privati | Dati Coerenti</p>
</div>
<div class="stats">
<div class="stat"><div class="stat-value">{total}</div><div class="stat-label">Lead Totali</div></div>
<div class="stat"><div class="stat-value">{alta_prio}</div><div class="stat-label">Alta Priorità</div></div>
<div class="stat"><div class="stat-value">{media_prio}</div><div class="stat-label">Media Priorità</div></div>
<div class="stat"><div class="stat-value">{con_tel}</div><div class="stat-label">Con Telefono</div></div>
<div class="stat"><div class="stat-value">{stats_portale.get("autoscout24", 0)}</div><div class="stat-label">AutoScout24</div></div>
<div class="stat"><div class="stat-value">{stats_portale.get("automobile", 0)}</div><div class="stat-label">Automobile.it</div></div>
</div>
<div class="leads">'''

        # FIX HTML COHERENCE: Ogni card usa UN SOLO oggetto Lead
        for lead in self.leads[:CONFIG_MAX_LEAD_DASHBOARD]:
            portale = lead.piattaforma.lower()
            prio_class = f"prio-{lead.priorita_conto_vendita}"

            # FIX HTML COHERENCE: Tutti i dati dalla STESSA istanza Lead
            html += f'''
<div class="lead">
<div class="lead-header">
<div class="lead-title">{lead.marca} {lead.modello}</div>
<span class="badge badge-{portale}">{lead.piattaforma}</span>
</div>
<div class="lead-meta">
<div class="meta-item"><span class="meta-label">Anno</span><span class="meta-value">{lead.anno}</span></div>
<div class="meta-item"><span class="meta-label">Km</span><span class="meta-value">{lead.km:,}</span></div>
<div class="meta-item"><span class="meta-label">Prezzo</span><span class="meta-value">{formatta_prezzo_euro(lead.prezzo)}</span></div>
<div class="meta-item"><span class="meta-label">Priorità</span><span class="meta-value {prio_class}">{lead.priorita_conto_vendita.upper()}</span></div>
<div class="meta-item"><span class="meta-label">Score</span><span class="meta-value">{lead.business_score}</span></div>
</div>
<a href="{lead.url}" target="_blank" rel="noopener" class="btn-open">🔗 Apri Annuncio</a>
</div>'''

        html += f'''
</div>
<div class="footer">
Broker Auto v7.0.1 | Generato: {datetime.now().strftime("%d/%m/%Y %H:%M")} | Query controllate + Dati coerenti
</div>
</div>
</body></html>'''

        filepath = os.path.join(CONFIG_OUTPUT_DIR, 'dashboard_broker.html')
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"   [OK] {filepath}")

# ============================================================================
# MENU SELEZIONE MARCHI
# ============================================================================

def mostra_menu_marchi() -> List[str]:
    print("\n" + "=" * 70)
    print("   SELEZIONE MARCHI")
    print("=" * 70)

    premium = [m for m in TUTTI_I_MARCHI if m in MARCHE_PREMIUM]
    altri = [m for m in TUTTI_I_MARCHI if m not in MARCHE_PREMIUM]

    print("\n   PREMIUM:")
    for i, m in enumerate(premium, 1):
        n = GeneratoreQueryConControllo.conta_query_per_marchio(m)
        print(f"   {i:2d}. {m:<15} ({n} modelli)")

    print("\n   GENERALISTI:")
    for i, m in enumerate(altri, len(premium) + 1):
        n = GeneratoreQueryConControllo.conta_query_per_marchio(m)
        print(f"   {i:2d}. {m:<15} ({n} modelli)")

    print("\n   OPZIONI: numeri (1,2,5), nomi (BMW,AUDI), ALL, TUTTI")

    while True:
        scelta = input("\n   Marchi: ").strip().upper()

        if not scelta:
            return premium[:5]

        if scelta == 'ALL':
            return premium

        if scelta == 'TUTTI':
            return TUTTI_I_MARCHI

        selezionati = []
        tutti = premium + altri

        for parte in scelta.replace(' ', '').split(','):
            try:
                idx = int(parte) - 1
                if 0 <= idx < len(tutti):
                    selezionati.append(tutti[idx])
                continue
            except ValueError:
                pass

            for m in TUTTI_I_MARCHI:
                if m.upper() == parte or m.upper().startswith(parte):
                    if m not in selezionati:
                        selezionati.append(m)
                    break

        if selezionati:
            riepilogo = GeneratoreQueryConControllo.riepilogo_marchi(selezionati)
            print(f"\n   Selezionati: {', '.join(selezionati)}")
            print(f"   Query totali: {riepilogo['totale']}")

            conf = input("\n   Confermi? [S/n]: ").strip().lower()
            if conf != 'n':
                return selezionati

# ============================================================================
# MAIN
# ============================================================================

async def main():
    print(f"""
{'='*70}
   BROKER AUTO AI v7.0.1 - QUERY CONTROL + COERENZA DATI
{'='*70}

   Portali: {', '.join(PORTALI_ATTIVI)}
   Query default: {DEFAULT_MAX_QUERIES}
   Max lead per query: {MAX_LEAD_PER_QUERY}

{'='*70}
""")

    # Step 1: Selezione marchi
    marchi_selezionati = mostra_menu_marchi()

    if not marchi_selezionati:
        print("\n   Nessun marchio. Uscita.")
        return

    # Step 2: Generazione query
    print("\n   Generazione query...")
    query_list = GeneratoreQueryConControllo.genera_query_da_marchi(marchi_selezionati)

    # Step 3: Riepilogo query
    GeneratoreQueryConControllo.mostra_riepilogo_query(query_list)

    # Step 4: FIX QUERY CONTROL - Chiedi numero query
    num_query = GeneratoreQueryConControllo.chiedi_numero_query(query_list)

    # Limita query
    query_limitate = query_list[:num_query]

    # Step 5: FIX QUERY REVIEW - Revisione opzionale
    query_approvate = GeneratoreQueryConControllo.review_and_edit_queries(query_limitate)

    if not query_approvate:
        print("\n   Nessuna query approvata. Uscita.")
        return

    # Step 6: Conferma finale
    # FIX: Stima lead più realistica considerando i filtri
    lead_teorici = len(query_approvate) * len(PORTALI_ATTIVI) * MAX_LEAD_PER_QUERY
    # Stima conservativa: ~40% viene filtrato (dealer, promo, prezzi fuori range, non coerenti)
    lead_stimati_min = int(lead_teorici * 0.4)
    lead_stimati_max = int(lead_teorici * 0.7)

    print(f"\n   " + "=" * 50)
    print(f"   RIEPILOGO FINALE")
    print(f"   " + "=" * 50)
    print(f"   Query da elaborare: {len(query_approvate)}")
    print(f"   Portali attivi: {len(PORTALI_ATTIVI)} ({', '.join(PORTALI_ATTIVI)})")
    print(f"   Max lead per query: {MAX_LEAD_PER_QUERY}")
    print(f"   Lead teorici (senza filtri): ~{lead_teorici}")
    print(f"   Lead stimati (con filtri): ~{lead_stimati_min}-{lead_stimati_max}")  # FIX: Stima realistica
    print(f"   Limite massimo totale: {CONFIG_MAX_LEAD_TOTALI}")

    conferma = input("\n   Avviare lo scraping? [S/n]: ").strip().lower()
    if conferma == 'n':
        print("\n   Operazione annullata.")
        return

    print()

    # Step 7: Esecuzione
    scraper = BrokerScraperV7(query_approvate)

    try:
        await scraper.esegui()

        print("\n   SALVATAGGIO:")
        scraper.salva_json()
        scraper.salva_csv()
        scraper.genera_dashboard()

        stats = scraper.stats
        # FIX: Statistiche più dettagliate
        con_telefono = len([l for l in scraper.leads if l.telefono_contatto])
        alta_prio = len([l for l in scraper.leads if l.priorita_conto_vendita == 'alta'])
        media_prio = len([l for l in scraper.leads if l.priorita_conto_vendita == 'media'])

        print(f"""
{'='*70}
   COMPLETATO v7.0!
{'='*70}
   Lead totali: {len(scraper.leads)}

   Per portale:
   - AutoScout24: {stats['per_portale'].get('autoscout24', 0)}
   - Automobile.it: {stats['per_portale'].get('automobile', 0)}

   Qualità lead:
   - Con telefono: {con_telefono}
   - Alta priorità: {alta_prio}
   - Media priorità: {media_prio}

   Filtrati/Scartati:
   - Dealer esclusi: {stats.get('dealer_esclusi', 0)}
   - Promo esclusi: {stats.get('promo_esclusi', 0)}
   - Non coerenti con query: {stats.get('non_coerenti', 0)}
   - Errori: {stats.get('errori', 0)}
{'='*70}
""")
    except KeyboardInterrupt:
        print("\n   Interrotto.")
        scraper.post_elaborazione()
        scraper.salva_json()
        scraper.genera_dashboard()
    except Exception as e:
        logger.exception("Errore:")

    input("   ENTER per chiudere...")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n   Bye!")
