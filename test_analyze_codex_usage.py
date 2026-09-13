"""Accounting regression tests; run: python3 -m unittest discover -s PATH."""
import unittest
from analyze_codex_usage import Counter, aggregate

def u(i, c, o, r=0):
    return dict(input_tokens=i, cached_input_tokens=c, output_tokens=o,
                reasoning_output_tokens=r, total_tokens=i+o)

class AccountingTests(unittest.TestCase):
    def test_cumulative_repeat_and_resume(self):
        c=Counter()
        a,s=c.consume(dict(total_token_usage=u(100,60,10),last_token_usage=u(100,60,10)))
        self.assertEqual(a['input_tokens'],100)
        self.assertIsNone(c.consume(dict(total_token_usage=u(100,60,10),last_token_usage=u(100,60,10)))[0])
        a,s=c.consume(dict(total_token_usage=u(140,90,15),last_token_usage=u(40,30,5)))
        self.assertEqual((a['input_tokens'],s),(40,'cumulative_verified'))

    def test_initial_inherited_total_not_charged(self):
        a,s=Counter().consume(dict(total_token_usage=u(1000,900,100),last_token_usage=u(50,40,5)))
        self.assertEqual(a['input_tokens'],50)

    def test_reset_only_last(self):
        c=Counter(); c.consume(dict(total_token_usage=u(1000,900,100),last_token_usage=u(1000,900,100)))
        a,s=c.consume(dict(total_token_usage=u(100,70,10),last_token_usage=u(30,20,3)))
        self.assertEqual((a['input_tokens'],s),(30,'counter_reset'))

    def test_context_placeholder_not_billable(self):
        c=Counter();a=u(0,0,0);a['total_tokens']=19396
        self.assertIsNone(c.consume(dict(total_token_usage=a,last_token_usage=a))[0])

    def test_cache_and_reasoning_not_added_twice(self):
        row={'input_tokens':1000000,'uncached_input_tokens':400000,
             'cached_input_tokens':600000,'output_tokens':10000,'reasoning_tokens':3000,
             'total_tokens':1010000,'session_id':'synthetic','unit_kind':'llm_call'}
        result=aggregate([row])
        self.assertEqual(result['input_tokens'],1000000)
        self.assertEqual(result['output_tokens'],10000)
        self.assertEqual(result['total_tokens'],1010000)
        self.assertEqual(result['cache_hit_ratio'],.6)
        self.assertFalse(any('cost' in key or 'pricing' in key for key in result))

    def test_invalid_subset_fails(self):
        with self.assertRaises(AssertionError):
            Counter().consume(dict(total_token_usage=u(10,11,1),last_token_usage=u(10,11,1)))

if __name__=='__main__':
    unittest.main()
