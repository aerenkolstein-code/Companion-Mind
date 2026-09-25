import copy
import json
import unittest

from companion_mind.connector_s3.docs_plan import (
    parse_document, replace_unique, insert_text, delete_paragraph, rollback,
    empty_created_document, utf16,
)
from companion_mind.connector_s3.policy import Denied


def document(text, revision='revision-1'):
    body, cursor = [{'endIndex': 1, 'sectionBreak': {'sectionStyle': {}}}], 1
    for paragraph in text.splitlines(keepends=True):
        end = cursor + utf16(paragraph)
        body.append({'startIndex': cursor, 'endIndex': end, 'paragraph': {
            'paragraphStyle': {'namedStyleType': 'NORMAL_TEXT', 'direction': 'LEFT_TO_RIGHT'},
            'elements': [{'startIndex': cursor, 'endIndex': end, 'textRun': {'content': paragraph, 'textStyle': {}}}]}})
        cursor = end
    return {'documentId': 'synthetic-doc', 'revisionId': revision, 'suggestionsViewMode': 'SUGGESTIONS_INLINE',
            'tabs': [{'tabProperties': {'tabId': 't.0', 'index': 0}, 'documentTab': {'body': {'content': body}}}]}


def snapshot(text, revision='revision-1'):
    return parse_document(document(text, revision), 'synthetic-doc')


def apply_synthetic(plan, current):
    """Tiny independent request interpreter: UTF16 byte ranges + revision gate."""
    request = json.loads(plan.request_bytes)
    if request['writeControl']['requiredRevisionId'] != current.revision:
        raise Denied('SYNTHETIC_PROVIDER_CONFLICT')
    raw = current.text.encode('utf-16-le')
    for operation in request['requests']:
        if 'deleteContentRange' in operation:
            span = operation['deleteContentRange']['range']
            lo, hi = 2 * (span['startIndex'] - 1), 2 * (span['endIndex'] - 1)
            if hi > len(raw) - 2:
                raise AssertionError('attempted final newline removal')
            raw = raw[:lo] + raw[hi:]
        else:
            insert = operation['insertText']
            index = 2 * (insert['location']['index'] - 1)
            raw = raw[:index] + insert['text'].encode('utf-16-le') + raw[index:]
    return snapshot(raw.decode('utf-16-le'), current.revision + '-next')


class DocsPlans(unittest.TestCase):
    def test_unicode_edit_and_real_restore_algorithm(self):
        before = snapshot('中文 😀 第一段\n第二段\n')
        plan = replace_unique(before, '第一段', '精确改动')
        after = apply_synthetic(plan, before)
        plan.verify(after)
        self.assertEqual(after.text, '中文 😀 精确改动\n第二段\n')
        inverse = rollback(plan, after)
        self.assertEqual(json.loads(inverse.request_bytes)['writeControl']['requiredRevisionId'], after.revision)
        restored = apply_synthetic(inverse, after)
        self.assertEqual(restored.text, before.text)
        self.assertEqual(restored.style_hash, before.style_hash)

    def test_delete_middle_paragraph_and_restore(self):
        before = snapshot('keep\nremove 😀\nlast\n')
        plan = delete_paragraph(before, 1)
        after = apply_synthetic(plan, before)
        self.assertEqual(after.text, 'keep\nlast\n')
        self.assertEqual(apply_synthetic(rollback(plan, after), after).text, before.text)

    def test_clear_created_doc_keeps_mandatory_newline(self):
        before = snapshot('synthetic C\n😀\n')
        plan = empty_created_document(before)
        self.assertEqual(apply_synthetic(plan, before).text, '\n')

    def test_insert_into_empty_document_and_restore(self):
        before = snapshot('\n')
        plan = insert_text(before, 0, 'local deterministic summary\n')
        after = apply_synthetic(plan, before)
        self.assertEqual(apply_synthetic(rollback(plan, after), after).text, '\n')

    def test_stale_revision_provider_conflict_does_not_apply(self):
        before = snapshot('hello\n')
        plan = replace_unique(before, 'hello', 'changed')
        with self.assertRaisesRegex(Denied, 'SYNTHETIC_PROVIDER_CONFLICT'):
            apply_synthetic(plan, snapshot('externally changed\n', 'other-revision'))

    def test_rollback_rejects_external_edit(self):
        plan = replace_unique(snapshot('hello\n'), 'hello', 'changed')
        with self.assertRaisesRegex(Denied, 'READBACK_MISMATCH'):
            rollback(plan, snapshot('changed by another actor\n', 'new-revision'))

    def test_rollback_rejects_external_style_change(self):
        plan = replace_unique(snapshot('hello\n'), 'hello', 'changed')
        after = document('changed\n', 'new-revision')
        after['tabs'][0]['documentTab']['documentStyle'] = {'background': {'color': {}}}
        with self.assertRaisesRegex(Denied, 'READBACK_MISMATCH'):
            rollback(plan, parse_document(after, 'synthetic-doc'))

    def test_reject_nonunique_match_and_missing_revision(self):
        with self.assertRaisesRegex(Denied, 'MATCH_NOT_UNIQUE'):
            replace_unique(snapshot('same same\n'), 'same', 'one')
        raw = document('hello\n')
        raw['revisionId'] = ''
        with self.assertRaisesRegex(Denied, 'REVISION_REQUIRED'):
            parse_document(raw, 'synthetic-doc')

    def test_reject_rich_structures_suggestions_and_multiple_tabs(self):
        original = document('hello\n')
        variants = []
        raw = copy.deepcopy(original)
        raw['tabs'].append(copy.deepcopy(raw['tabs'][0])); variants.append(raw)
        raw = copy.deepcopy(original)
        raw['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['textRun']['suggestedInsertionIds'] = ['s']
        variants.append(raw)
        raw = copy.deepcopy(original)
        raw['tabs'][0]['documentTab']['body']['content'].append({'table': {}}); variants.append(raw)
        raw = copy.deepcopy(original)
        raw['tabs'][0]['documentTab']['headers'] = {'h': {}}; variants.append(raw)
        for raw in variants:
            with self.subTest(raw_shape=list(raw)):
                with self.assertRaises(Denied):
                    parse_document(raw, 'synthetic-doc')

    def test_reject_wrong_resource_bad_utf16_indices_and_large_text(self):
        with self.assertRaisesRegex(Denied, 'DOCUMENT_MISMATCH'):
            parse_document(document('hello\n'), 'wrong-doc')
        raw = document('😀\n')
        raw['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['endIndex'] -= 1
        with self.assertRaisesRegex(Denied, 'INDEX_MISMATCH'):
            parse_document(raw, 'synthetic-doc')
        with self.assertRaisesRegex(Denied, 'TEXT_LIMIT'):
            insert_text(snapshot('\n'), 0, 'x' * 32768)

    def test_no_text_in_default_object_representation(self):
        source = snapshot('synthetic-secret-marker\n')
        plan = insert_text(source, 0, 'edit')
        self.assertNotIn('synthetic-secret-marker', repr(source))
        self.assertNotIn('synthetic-secret-marker', repr(plan))


if __name__ == '__main__':
    unittest.main()
