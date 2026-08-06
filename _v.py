import logging, os, json
logging.basicConfig(level=logging.WARNING)
from adwatch import crm_import as ci
H=os.path.expanduser('~/Downloads/')
print('provenance:', ci.backfill_websites(H+'adwatch_crm_export.json'))
from adwatch.identity import website
r = website.verify_batch(limit=60)
print('BATCH:', json.dumps(r, indent=1, ensure_ascii=False))
print('OVERVIEW:', json.dumps(website.overview(), indent=1, ensure_ascii=False))
