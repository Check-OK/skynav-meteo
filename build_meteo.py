"""Météo Sky-Nav : prévisions AROME (Météo-France) converties en images.

Chaque heure de prévision devient quelques images WebP (sans perte), découpés en tuiles,
que l'appli télécharge à la demande et dessine sur sa carte :

- ``sfc`` : vent à 10 m (est, nord) et rafales ;
- ``wx``  : pluie, nuages, température à 2 m ;
- ``cv``  : instabilité (CAPE), base des cumulus, humidité ;
- ``cl``  : bas et haut de la première couche de nuages (humidité ≥ 95 %
  aux hauteurs du modèle), plafond des thermiques (couche limite) ;
- ``hNNNN`` : vent à NNNN m au-dessus du sol (est, nord).

Chaque canal est un octet : valeur = min + octet / 255 × (max − min),
avec une racine carrée pour la pluie et la CAPE (plus de finesse dans les
petites valeurs). Le fichier ``manifest.json`` décrit tout ça à l'appli.

Données : Météo-France, AROME 0,025°, Licence Ouverte 2.0 (Etalab).

Usage :
    python build_meteo.py --out DOSSIER [--run 2026-11-16T09:00:00Z]
                          [--hours 30] [--cache DOSSIER]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import urllib.request
from pathlib import Path

import eccodes as ec
import numpy as np
from PIL import Image

BASE = 'https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt'
# Paquets de 6 h (dernière heure incluse).
PACKS = ['00H06H', '07H12H', '13H18H', '19H24H', '25H30H', '31H36H',
         '37H42H', '43H48H', '49H51H']

# Hauteurs (m au-dessus du sol) proposées pour le vent en altitude.
LEVELS = [100, 500, 1000, 1500, 2000, 3000]

TILE = 512

# Échelles de chaque canal : (nom, min, max, racine ?).
LAYERS = {
    'sfc': [('u', -150, 150, False), ('v', -150, 150, False),
            ('gust', 0, 200, False)],
    'wx': [('rain', 0, 50, True), ('clouds', 0, 100, False),
           ('temp', -40, 50, False)],
    'cv': [('cape', 0, 4000, True), ('cloudBase', 0, 5000, False),
           ('humidity', 0, 100, False)],
    # 4 000 m : pas de nuage (ou sommet) sous 3 000 m.
    'cl': [('cloudLow', 0, 4000, False), ('cloudTop', 0, 4000, False),
           ('thermalTop', 0, 4000, False)],
}

# Humidité au-delà de laquelle on considère qu'on est dans le nuage.
CLOUD_RH = 95.0
NO_CLOUD = 4000.0
for _lv in LEVELS:
    LAYERS[f'h{_lv}'] = [('u', -150, 150, False), ('v', -150, 150, False),
                         ('speed', 0, 200, False)]

MS_TO_KMH = 3.6


def url(run: str, pack: str, hours: str) -> str:
    return f'{BASE}/{run}/arome/0025/{pack}/arome__0025__{pack}__{hours}__{run}.grib2'


def exists(u: str) -> bool:
    try:
        req = urllib.request.Request(u, method='HEAD')
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def latest_run(hours: int) -> str:
    """Dernier réseau AROME dont tous les paquets utiles sont publiés."""
    now = dt.datetime.now(dt.timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0)
    t -= dt.timedelta(hours=t.hour % 3)
    last = packs_for(hours)[-1]
    for _ in range(8):
        run = t.strftime('%Y-%m-%dT%H:%M:%SZ')
        if exists(url(run, 'SP1', last)) and exists(url(run, 'HP1', last)):
            return run
        t -= dt.timedelta(hours=3)
    raise SystemExit('Aucun réseau AROME complet trouvé')


def packs_for(hours: int) -> list[str]:
    out = []
    for p in PACKS:
        out.append(p)
        if int(p[-3:-1]) >= hours:
            break
    return out


def download(u: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix('.part')
    print('  ↓', u.rsplit('/', 1)[-1], flush=True)
    with urllib.request.urlopen(u, timeout=600) as r, open(tmp, 'wb') as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    tmp.rename(dest)
    return dest


def step_hour(h) -> int | None:
    """Heure de fin d'un champ (« 3 » ou « 2-3 »)."""
    s = str(ec.codes_get(h, 'stepRange'))
    return int(s.split('-')[-1])


def read_fields(path: Path, wanted: dict) -> dict:
    """Champs voulus d'un fichier GRIB : {(nom, niveau, heure): tableau}.

    [wanted] : nom court → niveaux voulus (None : tous).
    """
    out = {}
    with open(path, 'rb') as f:
        while True:
            h = ec.codes_grib_new_from_file(f)
            if h is None:
                break
            try:
                name = ec.codes_get(h, 'shortName')
                if name == 'unknown':
                    continue
                level = ec.codes_get(h, 'level')
                if name not in wanted:
                    continue
                levels = wanted[name]
                if levels is not None and level not in levels:
                    continue
                ni, nj = ec.codes_get(h, 'Ni'), ec.codes_get(h, 'Nj')
                vals = ec.codes_get_values(h).reshape(nj, ni)
                miss = ec.codes_get(h, 'missingValue')
                vals = np.where(vals == miss, np.nan, vals).astype(np.float32)
                out[(name, level, step_hour(h))] = vals
            finally:
                ec.codes_release(h)
    return out


def encode(arr: np.ndarray, lo: float, hi: float, root: bool) -> np.ndarray:
    a = np.nan_to_num(arr, nan=lo)
    if root:
        a = np.sqrt(np.clip(a, 0, None))
        lo, hi = math.sqrt(lo), math.sqrt(hi)
    q = (a - lo) / (hi - lo) * 255
    return np.clip(np.rint(q), 0, 255).astype(np.uint8)


def save_layer(out: Path, layer: str, hour: int, chans: list[np.ndarray]):
    spec = LAYERS[layer]
    rgb = np.stack(
        [encode(c, lo, hi, root) for c, (_, lo, hi, root) in zip(chans, spec)],
        axis=-1,
    )
    nj, ni, _ = rgb.shape
    for ty in range(math.ceil(nj / TILE)):
        for tx in range(math.ceil(ni / TILE)):
            tile = rgb[ty * TILE:(ty + 1) * TILE, tx * TILE:(tx + 1) * TILE]
            d = out / layer / f'{hour:02d}'
            d.mkdir(parents=True, exist_ok=True)
            # WebP sans perte : 20 % plus léger que PNG, mêmes octets.
            Image.fromarray(tile).save(d / f'{tx}_{ty}.webp', lossless=True,
                                       method=4)


def total_clouds(l, m, h):
    # Couches supposées indépendantes : part de ciel couvert par au moins une.
    f = lambda x: np.clip(np.nan_to_num(x), 0, 100) / 100
    return 100 * (1 - (1 - f(l)) * (1 - f(m)) * (1 - f(h)))


def cloud_layer(rh_by_height: dict) -> tuple[np.ndarray, np.ndarray]:
    """Bas et haut (m sol) de la première couche nuageuse sous 3 000 m.

    [rh_by_height] : hauteur → humidité (%). Les passages du seuil sont
    interpolés entre deux hauteurs. Pas de nuage : bas = haut = 4 000 ;
    nuage jusqu'en haut de la colonne : haut = 4 000.
    """
    hs = sorted(rh_by_height)
    r = np.stack([np.nan_to_num(rh_by_height[h], nan=0) for h in hs])
    z = np.array(hs, dtype=np.float32)[:, None, None]
    wet = r >= CLOUD_RH
    shape = r.shape[1:]
    base = np.full(shape, NO_CLOUD, np.float32)
    top = np.full(shape, NO_CLOUD, np.float32)
    found = np.zeros(shape, bool)
    ended = np.zeros(shape, bool)
    for k in range(len(hs)):
        start = wet[k] & ~found
        if k == 0:
            base[start] = z[0, 0, 0]
        else:
            f = np.clip((CLOUD_RH - r[k - 1]) / np.maximum(r[k] - r[k - 1], 1e-3),
                        0, 1)
            zb = z[k - 1, 0, 0] + f * (z[k, 0, 0] - z[k - 1, 0, 0])
            base[start] = zb[start]
        found |= start
        stop = found & ~ended & ~wet[k]
        if k > 0:
            f = np.clip((r[k - 1] - CLOUD_RH) / np.maximum(r[k - 1] - r[k], 1e-3),
                        0, 1)
            zt = z[k - 1, 0, 0] + f * (z[k, 0, 0] - z[k - 1, 0, 0])
            top[stop] = zt[stop]
        ended |= stop
    return base, top


def build(run: str, hours: int, out: Path, cache: Path):
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    run_t = dt.datetime.strptime(run, '%Y-%m-%dT%H:%M:%SZ').replace(
        tzinfo=dt.timezone.utc)
    sfc_wanted = {'10u': None, '10v': None, 'max_i10fg': None, 'tp': None,
                  '2t': None, '2r': None}
    sp2_wanted = {'2d': None, 'lcc': None, 'mcc': None, 'hcc': None,
                  'CAPE_INS': None, 'blh': None}
    # À 100 m, Météo-France range le vent sous « 100u » / « 100v ».
    hp_wanted = {'u': set(LEVELS), 'v': set(LEVELS), '100u': None,
                 '100v': None, 'r': None}
    made_levels = set()
    made_cl = False
    prev_tp = None
    done_hours = []
    geo = None
    for pack in packs_for(hours):
        print(f'Paquet {pack}', flush=True)
        f1 = download(url(run, 'SP1', pack), cache / f'SP1_{pack}.grib2')
        f2 = download(url(run, 'SP2', pack), cache / f'SP2_{pack}.grib2')
        fh = download(url(run, 'HP1', pack), cache / f'HP1_{pack}.grib2')
        if geo is None:
            geo = grid_info(f1)
        a = read_fields(f1, sfc_wanted)
        b = read_fields(f2, sp2_wanted)
        c = read_fields(fh, hp_wanted)
        steps = sorted({k[2] for k in a if k[0] == '10u'})
        for s in steps:
            if s > hours or s in done_hours:
                continue
            g = lambda d, n, lv=None: next(
                (v for (k, l, t), v in d.items()
                 if k == n and t == s and (lv is None or l == lv)), None)
            u, v = g(a, '10u'), g(a, '10v')
            if u is None or v is None:
                continue
            gust = g(a, 'max_i10fg')
            if gust is None:
                gust = np.hypot(u, v)
            # Pluie de l'heure : cumul depuis le début, moins l'heure d'avant.
            tp = g(a, 'tp')
            rain = np.zeros_like(u) if tp is None else (
                tp if prev_tp is None else np.clip(tp - prev_tp, 0, None))
            if tp is not None:
                prev_tp = tp
            t2 = g(a, '2t')
            d2 = g(b, '2d')
            rh = g(a, '2r')
            temp = (t2 - 273.15) if t2 is not None else np.zeros_like(u)
            base = (np.clip((t2 - d2) * 125, 0, None)
                    if t2 is not None and d2 is not None else np.zeros_like(u))
            clouds = total_clouds(g(b, 'lcc'), g(b, 'mcc'), g(b, 'hcc')) \
                if g(b, 'lcc') is not None else np.zeros_like(u)
            cape = g(b, 'CAPE_INS')
            if cape is None:
                cape = np.zeros_like(u)
            save_layer(out, 'sfc', s, [u * MS_TO_KMH, v * MS_TO_KMH,
                                       gust * MS_TO_KMH])
            save_layer(out, 'wx', s, [rain, clouds, temp])
            save_layer(out, 'cv', s, [cape, base,
                                      rh if rh is not None else np.zeros_like(u)])
            rh_h = {l: v for (k, l, t), v in c.items() if k == 'r' and t == s}
            if rh_h:
                low, top = cloud_layer(rh_h)
                blh = g(b, 'blh')
                save_layer(out, 'cl', s, [
                    low, top,
                    np.clip(np.nan_to_num(blh), 0, 4000) if blh is not None
                    else np.zeros_like(u)])
                made_cl = True
            for lv in LEVELS:
                hu, hv = g(c, 'u', lv), g(c, 'v', lv)
                if hu is None or hv is None:
                    hu, hv = g(c, f'{lv}u'), g(c, f'{lv}v')
                if hu is None or hv is None:
                    continue
                save_layer(out, f'h{lv}', s, [hu * MS_TO_KMH, hv * MS_TO_KMH,
                                              np.hypot(hu, hv) * MS_TO_KMH])
                made_levels.add(lv)
            done_hours.append(s)
            print(f'  heure {s:02d} ok', flush=True)
        for f in (f1, f2, fh):
            if os.environ.get('METEO_KEEP') != '1':
                f.unlink(missing_ok=True)
    manifest = {
        'model': 'arome',
        'source': 'Météo-France AROME 0,025° (Licence Ouverte 2.0)',
        'run': run,
        'times': [(run_t + dt.timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M:%SZ')
                  for h in sorted(done_hours)],
        'hours': sorted(done_hours),
        'tile': TILE,
        # Seulement les hauteurs vraiment produites.
        'levels': sorted(made_levels),
        **geo,
        'layers': {k: [{'name': n, 'min': lo, 'max': hi, 'sqrt': root}
                       for n, lo, hi, root in v] for k, v in LAYERS.items()
                   if (not k.startswith('h') or int(k[1:]) in made_levels)
                   and (k != 'cl' or made_cl)},
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False))
    print('Fini :', len(done_hours), 'heures')


def grid_info(path: Path) -> dict:
    with open(path, 'rb') as f:
        h = ec.codes_grib_new_from_file(f)
        try:
            lon0 = ec.codes_get(h, 'longitudeOfFirstGridPointInDegrees')
            return {
                'nx': ec.codes_get(h, 'Ni'),
                'ny': ec.codes_get(h, 'Nj'),
                # Première ligne au nord, première colonne à l'ouest.
                'north': ec.codes_get(h, 'latitudeOfFirstGridPointInDegrees'),
                'west': lon0 - 360 if lon0 > 180 else lon0,
                'south': ec.codes_get(h, 'latitudeOfLastGridPointInDegrees'),
                'east': ec.codes_get(h, 'longitudeOfLastGridPointInDegrees'),
                'step': ec.codes_get(h, 'iDirectionIncrementInDegrees'),
            }
        finally:
            ec.codes_release(h)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--run')
    p.add_argument('--hours', type=int, default=30)
    p.add_argument('--cache', type=Path, default=Path('grib-cache'))
    a = p.parse_args()
    run = a.run or latest_run(a.hours)
    print('Réseau', run, flush=True)
    build(run, a.hours, a.out / run, a.cache)


if __name__ == '__main__':
    sys.exit(main())
