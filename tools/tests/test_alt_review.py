"""Accessibility-only revisions must never absorb visual or evidence changes."""
import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from studio_lib.illustration_workflow import verify_alt_change

class AltReviewTests(unittest.TestCase):
    def setUp(self):
        self.before={'text':{'alt':'Draw a box','title':'A','caption':'A control','labels':{'button':'Allow'}},'design':{'kind':'interface'},'references':[{'image_sha256':'original'}],'sources':['paragraph'],'facts':[{'claim':'original'}],'rules':{'style':'notebook-pen-v1'},'language':'zh-CN','prompt_sha256':'old'}
        self.after=copy.deepcopy(self.before);self.after['text']['alt']='A request with an Allow button';self.after['prompt_sha256']='new'
    def test_only_alt_and_derived_prompt_can_change(self):
        verify_alt_change(self.before,self.after,'Compared the request and Allow label against the same artwork.')
    def test_reject_changed_visual_labels_caption_and_title(self):
        for key in ('labels','caption','title'):
            with self.subTest(key=key):
                value=copy.deepcopy(self.after);value['text'][key]='different'
                with self.assertRaises(Exception):verify_alt_change(self.before,value,'review')
    def test_reject_changed_source_fact_reference_style_or_language(self):
        for key in ('design','references','sources','facts','rules','language'):
            with self.subTest(key=key):
                value=copy.deepcopy(self.after);value[key]='different'
                with self.assertRaises(Exception):verify_alt_change(self.before,value,'review')
    def test_requires_actual_change(self):
        with self.assertRaises(Exception):verify_alt_change(self.before,self.before,'review')
    def test_requires_review(self):
        with self.assertRaises(Exception):verify_alt_change(self.before,self.after,'')
    def test_requires_nonempty_description(self):
        self.after['text']['alt']=''
        with self.assertRaises(Exception):verify_alt_change(self.before,self.after,'review')
if __name__=='__main__':unittest.main()
