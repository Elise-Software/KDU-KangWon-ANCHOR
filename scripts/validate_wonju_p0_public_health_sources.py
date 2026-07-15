"""Validate that P0-DATA-03 evidence stays inside the approved seed scope."""
from __future__ import annotations
import argparse, csv, hashlib, json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def rows(p):
    with p.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def write(p, rs):
    p.parent.mkdir(parents=True,exist_ok=True); cols=['check','source_url','detail']
    with p.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(rs)
def main():
 p=argparse.ArgumentParser();p.add_argument('--collection-report',type=Path,default=ROOT/'data/collected/public_health/processed/p0_data_03_collection_report.json');p.add_argument('--source-pages',type=Path,default=ROOT/'data/collected/public_health/processed/p0_data_03_source_pages.csv');p.add_argument('--page-evidence',type=Path,default=ROOT/'data/collected/public_health/processed/p0_data_03_page_evidence.csv');p.add_argument('--seed-config',type=Path,default=ROOT/'config/p0_data_03_seed_urls.json');p.add_argument('--raw-dir',type=Path,default=ROOT/'data/collected/public_health/raw');p.add_argument('--output-dir',type=Path,default=ROOT/'data/processed/public_health');p.add_argument('--strict',action='store_true');a=p.parse_args()
 seeds={x['url'] for x in json.loads(a.seed_config.read_text(encoding='utf-8'))}; pages=rows(a.source_pages); evidence=rows(a.page_evidence); evid={x['source_url'] for x in evidence}; errors=[];warnings=[]
 urls={x['source_url'] for x in pages}
 for u in seeds-urls:errors.append({'check':'seed_present','source_url':u,'detail':'missing source_pages record'})
 for u in urls-seeds:errors.append({'check':'seed_scope','source_url':u,'detail':'not an approved seed'})
 for r in pages:
  u=r['source_url']; raw=ROOT/r.get('raw_html','')
  if r.get('status')!='collected':errors.append({'check':'fetch_status','source_url':u,'detail':r.get('status','')});continue
  if not raw.exists():errors.append({'check':'raw_html','source_url':u,'detail':'missing'})
  elif hashlib.sha256(raw.read_bytes()).hexdigest()!=r.get('sha256'):errors.append({'check':'sha256','source_url':u,'detail':'mismatch'})
  if u not in evid:errors.append({'check':'evidence_link','source_url':u,'detail':'missing'})
  if r.get('tls_compat_retry_used','').lower()=='true':warnings.append({'check':'tls_compat_retry','source_url':u,'detail':r.get('original_tls_error','')})
 write(a.output_dir/'source_validation_errors.csv',errors);write(a.output_dir/'source_validation_warnings.csv',warnings)
 report={'seed_count':len(seeds),'source_page_count':len(pages),'evidence_page_count':len(evidence),'error_count':len(errors),'warning_count':len(warnings),'integrity_checks':{'all_seeds_present':not(seeds-urls),'only_seed_urls_used':not(urls-seeds),'all_raw_html_and_sha256_valid':not any(x['check'] in {'raw_html','sha256'} for x in errors),'all_pages_linked_to_evidence':not any(x['check']=='evidence_link' for x in errors)},'integrity_checks_passed':not errors,'tls_compat_retry_pages':[x['source_url'] for x in warnings]}
 (a.output_dir/'source_validation_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2));return 1 if a.strict and errors else 0
if __name__=='__main__':raise SystemExit(main())
