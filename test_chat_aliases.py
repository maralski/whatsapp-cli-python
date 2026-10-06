"""Synthetic contact metadata only; no account connection or real contacts."""
from contextlib import closing
import json
import sqlite3
import time
import types
from unittest import mock
import unittest

import whatsapp_backend as backend
import whatsapp_cli as cli
import test_direct_backend as fixtures
from test_direct_backend import FakeMessage, JID, SECRET

LID = '76543210987654321@lid'


class ChatAliasTests(unittest.TestCase):
    setUp = fixtures.DirectTests.setUp
    linked = fixtures.DirectTests.linked

    def mapping(self, rows):
        with closing(sqlite3.connect(self.store / 'session.db')) as db, db:
            db.execute('CREATE TABLE whatsmeow_lid_map(lid TEXT,pn TEXT)')
            db.executemany('INSERT INTO whatsmeow_lid_map VALUES (?,?)', rows)

    def test_unique_numeric_or_full_jid_mapping(self):
        for lid, phone in ((LID.split('@')[0], JID.split('@')[0]), (LID, JID)):
            with self.subTest(lid=lid):
                self.mapping([(lid, phone)])
                before = (self.store / 'session.db').read_bytes()
                self.assertEqual(backend.chat_aliases(self.store, JID), {JID, LID})
                self.assertEqual((self.store / 'session.db').read_bytes(), before)
                with closing(sqlite3.connect(self.store / 'session.db')) as db, db:
                    db.execute('DROP TABLE whatsmeow_lid_map')

    def test_missing_mapping_retains_only_phone(self):
        self.assertEqual(backend.chat_aliases(self.store, JID), {JID})

    def test_multiple_lids_for_phone_never_expand_scope(self):
        self.mapping([(LID, JID), ('76543210987654322@lid', JID)])
        self.assertEqual(backend.chat_aliases(self.store, JID), {JID})

    def test_reverse_conflict_never_expands_scope(self):
        self.mapping([(LID, JID), (LID.split('@')[0], '12025550129')])
        self.assertEqual(backend.chat_aliases(self.store, JID), {JID})

    def test_unrelated_or_malformed_lid_never_expands_scope(self):
        self.mapping([(LID, '12025550129'), ('name-not-a-lid', JID)])
        self.assertEqual(backend.chat_aliases(self.store, JID), {JID})

    def test_view_mapping_rejected(self):
        with closing(sqlite3.connect(self.store / 'session.db')) as db, db:
            db.execute(
                "CREATE VIEW whatsmeow_lid_map AS SELECT '76543210987654321@lid' AS lid,'12025550123@s.whatsapp.net' AS pn")
        self.assertEqual(backend.chat_aliases(self.store, JID), {JID})

    def test_generated_mapping_columns_rejected(self):
        with closing(sqlite3.connect(self.store / 'session.db')) as db, db:
            db.execute('CREATE TABLE whatsmeow_lid_map(x TEXT,lid TEXT GENERATED ALWAYS AS (x) VIRTUAL,pn TEXT)')
            db.execute('INSERT INTO whatsmeow_lid_map(x,pn) VALUES (?,?)', (LID, JID))
        self.assertEqual(backend.chat_aliases(self.store, JID), {JID})

    def test_account_lock_permission_denial_does_not_echo_details(self):
        with mock.patch.object(backend.os, 'open', side_effect=PermissionError(SECRET)):
            with self.assertRaises(backend.StoreAccessDenied) as error:
                backend.open_account_lock(self.store)
        self.assertNotIn(SECRET, str(error.exception))

    def test_verified_lid_events_scoped_cached_and_revoked(self):
        self.mapping([(LID, JID)])
        index = backend.TextIndex(self.store, JID, 10, aliases=backend.chat_aliases(self.store, JID))
        now = int(time.time())
        def live(identifier, chat):
            user, server = chat.split('@')
            source = types.SimpleNamespace(Chat=types.SimpleNamespace(User=user, Server=server), IsGroup=False, IsFromMe=False)
            return types.SimpleNamespace(Info=types.SimpleNamespace(MessageSource=source, ID=identifier, Timestamp=now),
                Message=FakeMessage('selected live'), IsEphemeral=False, IsViewOnce=False,
                IsViewOnceV2=False, IsViewOnceV2Extension=False, IsEdit=False)
        index.live(None, live('LIVE', LID))
        index.live(None, live('FOREIGN', '76543210987654322@lid'))
        def item(identifier, chat):
            return types.SimpleNamespace(message=types.SimpleNamespace(key=types.SimpleNamespace(ID=identifier, remoteJID=chat, fromMe=False),
                messageTimestamp=now, message=FakeMessage('selected history')))
        index.history(None, types.SimpleNamespace(Data=types.SimpleNamespace(conversations=[
            types.SimpleNamespace(ID=LID, ephemeralExpiration=0, messages=[item('HISTORY', LID), item('WRONG_KEY', '76543210987654322@lid')])
        ])))
        rows = cli.read_history(self.store / 'messages.sqlite3', (JID, now - 1, now + 1, 20))
        self.assertEqual({r['id'] for r in rows}, {'LIVE', 'HISTORY'})
        self.assertTrue(all(r['direction'] == 'received' for r in rows))
        index.payload('REVOKE', now, False, FakeMessage(protocolMessage=types.SimpleNamespace(type=0,
            key=types.SimpleNamespace(ID='HISTORY', remoteJID=LID))))
        rows = cli.read_history(self.store / 'messages.sqlite3', (JID, now - 1, now + 1, 20))
        self.assertEqual([r['id'] for r in rows], ['LIVE'])

    def test_parent_reports_pre_dispatch_store_denial(self):
        self.linked()
        with mock.patch.object(cli, 'run_bounded', return_value=(0, json.dumps({'error': 'store_access_denied', 'extra': SECRET}).encode())):
            with self.assertRaises(cli.SafeError) as error:
                cli.backend_operation('sync', self.store, jid=JID)
        self.assertEqual(error.exception.code, 'store_access_denied')
        self.assertNotIn(SECRET, str(error.exception))
