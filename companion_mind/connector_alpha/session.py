"""Manual single-file read and revocation lifecycle; no retry or refresh path."""
from __future__ import annotations

from copy import deepcopy
import json

from .contract import Denied, MIME, receipt, require


def _drive(value, binding):
    require(type(value) is dict and set(value) == {'id', 'mimeType', 'version', 'modifiedTime', 'trashed', 'isAppAuthorized'},
            'PROVIDER_RESPONSE_INVALID')
    require(value['id'] == binding.file_id and value['mimeType'] == MIME and value['trashed'] is False
            and value['isAppAuthorized'] is True, 'RESOURCE_BINDING_MISMATCH')
    require(type(value['version']) is str and value['version'] and type(value['modifiedTime']) is str
            and value['modifiedTime'], 'VERSION_UNAVAILABLE')
    return {'version': value['version'], 'modified_at': value['modifiedTime']}


def _docs(value, binding):
    require(type(value) is dict and set(value) == {'documentId', 'revisionId'}, 'PROVIDER_RESPONSE_INVALID')
    require(value['documentId'] == binding.file_id and type(value['revisionId']) is str and value['revisionId'],
            'RESOURCE_BINDING_MISMATCH')
    return value['revisionId']


def _coverage(body, binding):
    require(type(body) is dict and body.get('documentId') == binding.file_id, 'RESOURCE_BINDING_MISMATCH')
    tabs = body.get('tabs')
    if type(tabs) is not list or not tabs:
        return 'UNKNOWN', None
    def text_from_content(content):
        if type(content) is not list:
            return None
        output = []
        for block in content:
            if type(block) is not dict:
                return None
            if 'paragraph' in block and set(block) == {'paragraph'}:
                elements = block['paragraph'].get('elements') if type(block['paragraph']) is dict else None
                if type(elements) is not list: return None
                for element in elements:
                    run = element.get('textRun') if type(element) is dict and set(element) == {'textRun'} else None
                    if type(run) is not dict or type(run.get('content')) is not str: return None
                    output.append(run['content'])
            elif 'table' in block and set(block) == {'table'}:
                rows = block['table'].get('tableRows') if type(block['table']) is dict else None
                if type(rows) is not list: return None
                for row in rows:
                    cells = row.get('tableCells') if type(row) is dict else None
                    if type(cells) is not list: return None
                    for cell in cells:
                        nested = text_from_content(cell.get('content') if type(cell) is dict else None)
                        if nested is None: return None
                        output.append(nested)
            else:
                return None
        return ''.join(output)
    def tab_projection(tab):
        if type(tab) is not dict or set(tab) - {'tabProperties', 'documentTab', 'childTabs'}:
            return None
        doc = tab.get('documentTab')
        known = {'body', 'headers', 'footers', 'footnotes', 'documentStyle', 'namedStyles', 'lists',
                 'namedRanges', 'inlineObjects', 'positionedObjects', 'suggestedDocumentStyleChanges'}
        if type(doc) is not dict or 'body' not in doc or set(doc) - known:
            return None
        text = text_from_content(doc['body'].get('content') if type(doc['body']) is dict else None)
        if text is None: return None
        children = tab.get('childTabs', [])
        if type(children) is not list:
            return None
        projected_children = [tab_projection(child) for child in children]
        if any(child is None for child in projected_children):
            return None
        # Copy only the documented content-bearing structures, never unknown raw fields.
        return {'text': text, 'child_tabs': projected_children}
    projected = [tab_projection(tab) for tab in tabs]
    if any(tab is None for tab in projected):
        return 'PARTIAL', None
    return 'FULL', {'document_id': binding.file_id, 'revision_id': body.get('revisionId'), 'tabs': projected}


class ConnectorSession:
    def __init__(self, binding, broker, transport):
        self.binding, self.broker, self.transport = binding, broker, transport
        self.closed = False

    def read_once(self):
        require(not self.closed, 'LOCAL_AUTHORIZATION_CLOSED')
        token = self.broker.acquire()
        try:
            identity = self.transport.read('identity', token).get('user')
            require(type(identity) is dict and identity.get('emailAddress') == self.binding.subject_email
                    and identity.get('permissionId') == self.binding.subject_permission_id, 'IDENTITY_MISMATCH')
            m0 = _drive(self.transport.read('drive_before', token), self.binding)
            d0 = _docs(self.transport.read('docs_before', token), self.binding)
            body = self.transport.read('body', token)
            d1 = _docs(self.transport.read('docs_after', token), self.binding)
            m1 = _drive(self.transport.read('drive_after', token), self.binding)
            require((m0, d0) == (m1, d1) and body.get('revisionId') == d0, 'VERSION_CHANGED')
            require(token not in json.dumps(body, ensure_ascii=False), 'SECRET_OUTPUT_BLOCKED')
            coverage, content = _coverage(body, self.binding)
            require(not self.closed and not getattr(self.broker, 'closed', False), 'GRANT_INVALIDATED_IN_FLIGHT')
            status = 'SUCCESS' if coverage == 'FULL' else 'UNKNOWN'
            return {'content': content, 'receipt': receipt(status, 'EXACT_READ_AS_OF' if status == 'SUCCESS' else 'COVERAGE_INCOMPLETE',
                    google_reads=6, credential_dereferences=1, content_delivered=status == 'SUCCESS',
                    coverage=coverage, drive_version=m0['version'], docs_revision=d0)}
        finally:
            token = None

    def revoke(self):
        require(not self.closed, 'LOCAL_AUTHORIZATION_CLOSED')
        token = None
        try:
            token = self.broker.acquire()
        finally:
            # Closing is unconditional: unavailable/expired credentials must never
            # leave a usable local session behind.
            self.closed = True
            try:
                self.broker.close_and_delete()
            except Exception:
                # The in-memory gate remains closed even if OS deletion fails.
                pass
        if token is None:
            return receipt('REVOKED', 'LOCAL_CLOSED_CREDENTIAL_UNAVAILABLE', remote_revocations=0,
                           credential_delete='UNKNOWN')
        try:
            self.transport.revoke(token)
            return receipt('REVOKED', 'LOCAL_CLOSED_REMOTE_REVOKED', remote_revocations=1,
                           credential_delete='UNKNOWN')
        except Exception:
            return receipt('REVOKED', 'LOCAL_CLOSED_REMOTE_REVOKE_UNKNOWN', remote_revocations=1,
                           credential_delete='ATTEMPTED')
        finally:
            token = None
