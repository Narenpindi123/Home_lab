"""Regression checks for replacing the retired Apify integration."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import apify_sources
import discovery_store
import local_job_sources as sources
from linkedin_api import Store, create_app


class LocalSourcesTests(unittest.TestCase):
    def test_dice_structured_data_preserves_date_and_country(self):
        payload={'@type':'JobPosting','title':'SOC Analyst','description':'<p>Monitor SIEM.</p>',
                 'hiringOrganization':{'name':'Example'},'datePosted':'2026-10-01',
                 'jobLocation':{'address':{'addressLocality':'Toronto','addressCountry':'CA'}}}
        job=sources.dice_detail('<script type="application/ld+json">'+json.dumps(payload)+'</script>',
                                'https://www.dice.com/job-detail/test')
        self.assertEqual(job['publishedAt'],'2026-10-01')
        self.assertEqual(job['location'],'Toronto, CA')
        self.assertEqual(job['jobDescription'],'Monitor SIEM.')

    def test_greenhouse_modified_date_is_not_publication_date(self):
        payload={'jobs':[{'title':'GRC Analyst','absolute_url':'https://example.com/job/1',
            'location':{'name':'Remote'},'content':'<p>Review controls.</p>','updated_at':'2026-10-09'}]}
        with patch.object(sources,'request',return_value=json.dumps(payload)):
            job=sources.board_jobs({'ats':'greenhouse','slug':'example'})[0]
        self.assertEqual(job['publishedAt'],'')
        self.assertEqual(job['_ats_platform'],'greenhouse')

    def test_rejects_untrusted_board_paths(self):
        for target in [{'ats':'greenhouse','slug':'../metadata'}, {'ats':'custom','slug':'example'},
                       {'ats':'ashby','slug':'https://127.0.0.1'}]:
            with self.assertRaises(ValueError):sources.normalize_input('ats',{'targets':[target]})

    def test_legacy_entrypoint_calls_only_local_sources(self):
        conn=sqlite3.connect(':memory:')
        discovery_store.init_schema(conn)
        with patch.object(apify_sources,'scrape_source',return_value=([],{'status':'success','errors':[]})) as fetch:
            self.assertEqual(apify_sources.scrape_jobs(conn),[])
        self.assertEqual([x.args[0] for x in fetch.call_args_list],['ats','dice'])
        self.assertFalse(json.loads(Path(__file__).with_name('apify_sources.json').read_text())['enabled'])
        conn.close()

    def test_local_api_routes_actor_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'runs.db')
            client=create_app(store,'local').test_client()
            response=client.post('/v1/sources/dice/runs',json={'limit':2},headers={'Authorization':'Bearer local'})
            self.assertEqual(response.status_code,201)
            run=response.json['data']
            self.assertEqual(run['actId'],'solidcode~dice-scraper')
            self.assertEqual(store.get(run['id'])['actor'],run['actId'])


if __name__=='__main__':unittest.main()
