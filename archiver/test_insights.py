import json
import unittest
from unittest.mock import patch
import archive as a
import chat_archive as chat
import archive_insights as insights
import test_chat
edge = test_chat.edge


class InsightTests(unittest.TestCase):
    setUp = test_chat.ChatTests.setUp
    tearDown = test_chat.ChatTests.tearDown
    def test_derived_actions_and_timeline_keep_source_and_limits_explicit(self):
        msg=edge('1',20,'a','!help @tester https://example.com')
        emote=edge('2',10,'b','Kappa');emote['node']['message']['fragments'][0]['emote']={'emoteID':'25'}
        with patch.object(chat,'page',return_value=([msg,emote],False)):
            chat.archive(self.row)
        db=a.connect()
        insights.observe(db,dict(json.loads(self.row['meta']),id=self.row['id']),{'id':'42'})
        db.commit()
        result=insights.process(db,self.row)
        self.assertEqual(result['messages'],2)
        self.assertEqual(result['actions'],4)
        metrics=json.loads((self.dest/'metrics.json').read_text())
        self.assertEqual(metrics['chat']['commands'],{'!help':1})
        self.assertIn('complete_subscription_history',metrics['coverage']['unavailable_from_this_source'])
        events=[json.loads(line) for line in (self.dest/'events.jsonl').read_text().splitlines()]
        self.assertEqual([x['offset_seconds'] for x in events],sorted(x['offset_seconds'] for x in events))
        self.assertEqual(sum(x['type']=='chat.message' for x in events),2)
        db.close()


if __name__=='__main__':unittest.main(verbosity=2)
