"""Envoie un réseau converti par build_meteo.py vers Cloudflare R2.

Les images d'abord, puis ``arome/latest.json`` (l'appli ne voit le nouveau
réseau qu'une fois tout en ligne). Garde les deux derniers réseaux.

Variables d'environnement (secrets GitHub) :
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

Usage :
    python upload_r2.py DOSSIER_DU_RESEAU
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config

KEEP_RUNS = 2


def client():
    return boto3.client(
        's3',
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        region_name='auto',
        config=Config(retries={'max_attempts': 5}, max_pool_connections=32),
    )


def main(run_dir: str):
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    model, run = manifest['model'], manifest['run']
    bucket = os.environ['R2_BUCKET']
    s3 = client()

    files = sorted(p for p in run_dir.rglob('*.webp'))
    print(f'{len(files)} images à envoyer', flush=True)

    def put(p: Path):
        key = f'{model}/{run}/{p.relative_to(run_dir).as_posix()}'
        s3.upload_file(str(p), bucket, key, ExtraArgs={
            'ContentType': 'image/webp',
            # Un réseau ne change jamais : cache long.
            'CacheControl': 'public, max-age=86400, immutable',
        })

    with cf.ThreadPoolExecutor(32) as ex:
        for i, _ in enumerate(ex.map(put, files), 1):
            if i % 500 == 0:
                print(f'  {i}', flush=True)

    body = json.dumps(manifest, ensure_ascii=False).encode()
    for key in (f'{model}/{run}/manifest.json', f'{model}/latest.json'):
        s3.put_object(Bucket=bucket, Key=key, Body=body,
                      ContentType='application/json',
                      CacheControl='public, max-age=300')
    print('Manifeste publié', flush=True)

    # Ménage : seuls les derniers réseaux restent.
    runs = set()
    for page in s3.get_paginator('list_objects_v2').paginate(
            Bucket=bucket, Prefix=f'{model}/', Delimiter='/'):
        for cp in page.get('CommonPrefixes', []):
            runs.add(cp['Prefix'].split('/')[1])
    old = sorted(runs)[:-KEEP_RUNS]
    for r in old:
        keys = []
        for page in s3.get_paginator('list_objects_v2').paginate(
                Bucket=bucket, Prefix=f'{model}/{r}/'):
            keys += [{'Key': o['Key']} for o in page.get('Contents', [])]
        for i in range(0, len(keys), 1000):
            s3.delete_objects(Bucket=bucket,
                              Delete={'Objects': keys[i:i + 1000]})
        print('Réseau effacé :', r, len(keys), 'fichiers', flush=True)


if __name__ == '__main__':
    main(sys.argv[1])
