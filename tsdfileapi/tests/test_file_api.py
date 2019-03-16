
"""

Run this as: python -m tsdfileapi.tests.test_file_api test-config.yaml

-------------------------------------------------------------------------------

Exploring Transfer-Encoding: chunked with a minimal python client.

From: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Transfer-Encoding

Data is sent in a series of chunks. The Content-Length header is omitted in this
case and at the beginning of each chunk you need to add the length of the current
chunk in hexadecimal format, followed by '\r\n' and then the chunk itself, followed
by another '\r\n'.

The terminating chunk is a regular chunk, with the exception that its length is zero.
It is followed by the trailer, which consists of a (possibly empty) sequence of
entity header fields.

E.g.

HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

7\r\n
Mozilla\r\n
9\r\n
Developer\r\n
7\r\n
Network\r\n
0\r\n
\r\n

tornado
so with tornado it _just works_ - but not the naive impl
the async method write data to the file, while waiting for the rest
this is _exactly what the video streaming needs.
behind nginx you need to set the following:
proxy_http_version 1.1;
proxy_request_buffering off;

From bdarnell:
sending an error requires a little-used feature of HTTP called "100-continue".
If the client supports 100-continue (curl-based clients do by default for large POSTS;
most others don't. I don't know if curl uses 100-continue with chunked requests)
and the tornado service uses @stream_request_body, then sending an error response
from prepare() will be received by the client before the body is uploaded

On 100-continue:
https://tools.ietf.org/html/rfc7231#page-50

So the HTTP Client should implement this...
Some background on python2.7 and requests
https://github.com/kennethreitz/requests/issues/713

"""

# pylint tends to be too pedantic regarding docstrings - we can decide in code review
# pylint: disable=missing-docstring
# test names are verbose...
# pylint: disable=too-many-public-methods
# method names are verbose in tests
# pylint: disable=invalid-name

import base64
import httplib
import json
import logging
import os
import random
import sys
import time
import unittest
import pwd
import uuid
import shutil
from datetime import datetime

import gnupg
import requests
import yaml
from sqlalchemy.exc import OperationalError
from tsdapiclient import fileapi

# monkey patch to avoid random error message
# https://github.com/isislovecruft/python-gnupg/issues/207
import gnupg._parsers
gnupg._parsers.Verify.TRUST_LEVELS["ENCRYPTION_COMPLIANCE_MODE"] = 23

# pylint: disable=relative-import
from tokens import gen_test_tokens, get_test_token_for_p12, gen_test_token_for_user
from ..db import session_scope, sqlite_init
from ..utils import project_import_dir, project_sns_dir, md5sum
from ..pgp import _import_keys


def lazy_file_reader(filename):
    with open(filename, 'r+') as f:
        while True:
            line = f.readline()
            if line == '':
                break
            else:
                yield line


def build_payload(config, dtype):
    gpg = gnupg.GPG(binary=config['gpg_binary'], homedir=config['gpg_homedir'],
                    keyring=config['gpg_keyring'], secring=config['gpg_secring'])
    key_id = config['public_key_id']
    _id = random.randint(1, 1000000)
    if dtype == 'ns':
        message = json.dumps({'submission_id': _id, 'consent': 'yes', 'age': 20,
                              'email_address': 'my2@email.com',
                              'national_id_number': '18101922351',
                              'phone_number': '4820666472',
                              'children_ages': '{"6", "70"}', 'var1': '{"val2"}'})
        encr = str(gpg.encrypt(message, key_id))
        data = {'form_id': 63332, 'submission_id': _id,
                'submission_timestamp': datetime.utcnow().isoformat(),
                'key_id': key_id, 'data': encr}
    elif dtype == 'generic':
        message = json.dumps({'x': 10, 'y': 'bla'})
        encr = str(gpg.encrypt(message, key_id))
        data = {'table_name': 'test1', 'submission_id': _id,
                'key_id': key_id, 'data': encr}
    return data


class TestFileApi(unittest.TestCase):

    @classmethod
    def pgp_encrypt_and_base64_encode(cls, string):
        gpg = _import_keys(cls.config)
        encrypted = gpg.encrypt(string, cls.config['public_key_id'], armor=False)
        encoded = base64.b64encode(encrypted.data)
        return encoded

    @classmethod
    def setUpClass(cls):

        try:
            with open(sys.argv[1]) as f:
                cls.config = yaml.load(f)
        except Exception as e:
            print e
            print "Missing config file?"
            sys.exit(1)

        # includes p19 - a random project number for integration testing
        cls.test_project = cls.config['test_project']
        cls.base_url = 'http://localhost' + ':' + str(cls.config['port']) + '/' + cls.test_project
        cls.data_folder = cls.config['data_folder']
        cls.example_csv = os.path.normpath(cls.data_folder + '/example.csv')
        cls.an_empty_file = os.path.normpath(cls.data_folder + '/an-empty-file')
        cls.example_codebook = json.loads(
            open(os.path.normpath(cls.data_folder + '/example-ns.json')).read())
        cls.test_user = cls.config['test_user']
        cls.test_group = cls.config['test_group']
        cls.uploads_folder = project_import_dir(cls.config['uploads_folder'], cls.config['test_project'])
        cls.uploads_folder_p12 = project_import_dir(cls.config['uploads_folder'], 'p12')
        cls.sns_uploads_folder = project_sns_dir(cls.config['sns_uploads_folder'],
                                                 cls.config['test_project'],
                                                 cls.config['test_keyid'],
                                                 cls.config['test_formid'],
                                                 test=True)

        # endpoints
        cls.upload = cls.base_url + '/files/upload'
        cls.sns_upload = cls.base_url + '/sns/' + cls.config['test_keyid'] + '/' + cls.config['test_formid']
        cls.upload_sns_wrong = cls.base_url + '/sns/' + 'WRONG' + '/' + cls.config['test_formid']
        cls.stream = cls.base_url + '/files/stream'
        cls.upload_stream = cls.base_url + '/files/upload_stream'
        cls.export = cls.base_url + '/files/export'
        cls.resumables = cls.base_url + '/files/resumables'
        cls.test_project = cls.test_project

        # auth tokens
        global TEST_TOKENS
        TEST_TOKENS = gen_test_tokens(cls.config)
        global P12_TOKEN
        P12_TOKEN = get_test_token_for_p12(cls.config)

        # example data
        cls.example_tar = os.path.normpath(cls.data_folder + '/example.tar')
        cls.example_tar_gz = os.path.normpath(cls.data_folder + '/example.tar.gz')
        cls.enc_symmetric_secret = cls.pgp_encrypt_and_base64_encode('tOg1qbyhRMdZLg==')
        cls.enc_hex_aes_key = cls.pgp_encrypt_and_base64_encode('ed6d4be32230db647bc63627f98daba0ac1c5d04ab6d1b44b74501ff445ddd97')
        cls.hex_aes_iv = 'a53c9b54b5f84e543b592050c52531ef'
        cls.example_aes = os.path.normpath(cls.data_folder + '/example.csv.aes')
        # tar -cf - totar3 | openssl enc -aes-256-cbc -a -pass file:<( echo $PW ) > example.tar.aes
        cls.example_tar_aes = os.path.normpath(cls.data_folder + '/example.tar.aes')
        # tar -cf - totar3 | gzip -9 | openssl enc -aes-256-cbc -a -pass file:<( echo $PW ) > example.tar.gz.aes
        cls.example_tar_gz_aes = os.path.normpath(cls.data_folder + '/example.tar.gz.aes')
        cls.example_gz = os.path.normpath(cls.data_folder + '/example.csv.gz')
        cls.example_gz_aes = os.path.normpath(cls.data_folder + '/example.csv.gz.aes')
        # openssl enc -aes-256-cbc -a -iv ${hex_aes_iv} -K ${hex_aes_key}
        cls.example_aes_with_key_and_iv = os.path.normpath(cls.data_folder + '/example.csv.aes-with-key-and-iv')
        cls.example_tar_aes_with_key_and_iv = os.path.normpath(cls.data_folder + '/example.tar.aes-with-key-and-iv')
        cls.example_tar_gz_aes_with_key_and_iv = os.path.normpath(cls.data_folder + '/example.tar.gz.aes-with-key-and-iv')
        cls.example_gz_aes_with_key_and_iv = os.path.normpath(cls.data_folder + '/example.csv.gz.aes-with-key-and-iv')
        # openssl enc -aes-256-cbc -iv ${hex_aes_iv} -K ${hex_aes_key}
        cls.example_binary_aes_with_key_and_iv = os.path.normpath(cls.data_folder + '/example.csv.binary-aes-with-key-and-iv')
        # resumables
        cls.resume_file1 = os.path.normpath(cls.data_folder + '/resume-file1')
        cls.resume_file2 = os.path.normpath(cls.data_folder + '/resume-file2')
        cls.test_upload_id = '96c68dad-8dc5-4076-9569-92394001d42a'
        # TODO: make this configurable
        # do not dist with package
        cls.large_file = os.path.normpath(cls.data_folder + '/large-file')


    @classmethod
    def tearDownClass(cls):
        uploaded_files = os.listdir(cls.uploads_folder)
        test_files = os.listdir(cls.config['data_folder'])
        today = datetime.fromtimestamp(time.time()).isoformat()[:10]
        file_list = ['streamed-example.csv', 'uploaded-example.csv',
                     'uploaded-example-2.csv', 'uploaded-example-3.csv',
                     'streamed-not-chunked', 'streamed-put-example.csv']
        for _file in uploaded_files:
            # TODO: eventually remove - still want to inspect them
            # manually while the data pipelines are in alpha
            if _file in ['totar', 'totar2', 'decrypted-aes.csv',
                         'totar3', 'totar4', 'ungz1', 'ungz-aes1',
                         'uploaded-example-2.csv', 'uploaded-example-3.csv']:
                continue
            if (_file in test_files) or (today in _file) or (_file in file_list):
                try:
                    os.remove(os.path.normpath(cls.uploads_folder + '/' + _file))
                except OSError as e:
                    logging.error(e)
                    continue
        sqlite_path = cls.config['uploads_folder'][cls.config['test_project']] + '/api-data.db'
        try:
            os.remove(sqlite_path)
        except OSError:
            print 'not tables to cleanup'
            return

    # Import Auth
    #------------

    def check_endpoints(self, headers):
        files = {'file': ('example.csv', open(self.example_csv))}
        for url in [self.upload, self.stream, self.upload_stream, self.upload]:
            resp = requests.put(url, headers=headers, files=files)
            self.assertEqual(resp.status_code, 401)
            resp = requests.post(url, headers=headers, files=files)
            self.assertEqual(resp.status_code, 401)
            resp = requests.patch(url, headers=headers, files=files)
            self.assertEqual(resp.status_code, 401)


    def test_A_mangled_valid_token_rejected(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['MANGLED_VALID']}
        self.check_endpoints(headers)


    def test_B_invalid_signature_rejected(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['INVALID_SIGNATURE']}
        self.check_endpoints(headers)


    def test_C_token_with_wrong_role_rejected(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['WRONG_ROLE']}
        self.check_endpoints(headers)


    def test_D_timed_out_token_rejected(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['TIMED_OUT']}
        self.check_endpoints(headers)


    def test_E_unauthenticated_request_rejected(self):
        headers = {}
        self.check_endpoints(headers)


    # uploading files and streams
    #----------------------------

    # multipart formdata endpoint

    def remove(self, target_uploads_folder, newfilename):
        try:
            _file = os.path.normpath(target_uploads_folder + '/' + newfilename)
            os.remove(_file)
        except OSError:
            pass


    def mp_fd(self, newfilename, target_uploads_folder, url, method):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        f = open(self.example_csv)
        files = {'file': (newfilename, f)}
        if method == 'POST':
            self.remove(target_uploads_folder, newfilename)
            resp = requests.post(url, files=files, headers=headers)
        elif method == 'PATCH':
            # not going to remove here, since we need to test non-idempotent uploads
            resp = requests.patch(url, files=files, headers=headers)
        elif method == 'PUT':
            # not going to remove, need to check that it is idempotent
            resp = requests.put(url, files=files, headers=headers)
        f.close()
        return resp


    def check_copied_sns_file_exists(self, filename):
        file = (self.sns_uploads_folder + '/' + filename)
        hidden_file = file.replace(self.config['public_key_id'], '.tsd/' + self.config['public_key_id'])
        self.assertTrue(os.path.lexists(hidden_file))


    def t_post_mp(self, uploads_folder, newfilename, url):
        target = os.path.normpath(uploads_folder + '/' + newfilename)
        resp = self.mp_fd(newfilename, uploads_folder, url, 'POST')
        self.assertEqual(resp.status_code, 201)
        uploaded_file = os.path.normpath(uploads_folder + '/' + newfilename)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file))


    def test_F_post_file_multi_part_form_data(self):
        self.t_post_mp(self.uploads_folder, 'uploaded-example.csv', self.upload)


    def test_F1_post_file_multi_part_form_data_sns(self):
        filename = 'sns-uploaded-example.csv'
        self.t_post_mp(self.sns_uploads_folder, filename, self.sns_upload)
        self.check_copied_sns_file_exists(filename)


    def test_FA_post_multiple_files_multi_part_form_data(self):
        newfilename1 = 'n1'
        newfilename2 = 'n2'
        try:
            os.remove(os.path.normpath(self.uploads_folder + '/' + newfilename1))
            os.remove(os.path.normpath(self.uploads_folder + '/' + newfilename2))
        except OSError:
            pass
        files = [('file', (newfilename1, open(self.example_csv, 'rb'), 'text/html')),
                 ('file', (newfilename2, open(self.example_csv, 'rb'), 'text/html'))]
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.post(self.upload, files=files, headers=headers)
        self.assertEqual(resp.status_code, 201)
        uploaded_file1 = os.path.normpath(self.uploads_folder + '/' + newfilename1)
        uploaded_file2 = os.path.normpath(self.uploads_folder + '/' + newfilename2)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file1))
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file2))
        files = [('file', (newfilename1, open(self.example_csv, 'rb'), 'text/html')),
                 ('file', (newfilename2, open(self.example_csv, 'rb'), 'text/html'))]
        resp2 = requests.post(self.upload, files=files, headers=headers)
        self.assertEqual(resp2.status_code, 201)
        self.assertNotEqual(md5sum(self.example_csv), md5sum(uploaded_file1))
        self.assertNotEqual(md5sum(self.example_csv), md5sum(uploaded_file2))


    def t_patch_mp(self, uploads_folder, newfilename, url):
        target = os.path.normpath(uploads_folder + '/' + newfilename)
        # need to get rid of previous round's file, if present
        self.remove(uploads_folder, newfilename)
        # first request - create a new file
        resp = self.mp_fd(newfilename, target, url, 'PATCH')
        self.assertEqual(resp.status_code, 201)
        uploaded_file = os.path.normpath(uploads_folder + '/' + newfilename)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file))
        # second request - PATCH should not be idempotent
        resp2 = self.mp_fd(newfilename, target, url, 'PATCH')
        self.assertEqual(resp2.status_code, 201)
        self.assertNotEqual(md5sum(self.example_csv), md5sum(uploaded_file))


    def test_G_patch_file_multi_part_form_data(self):
        self.t_patch_mp(self.uploads_folder, 'uploaded-example-2.csv', self.upload)


    def test_G1_patch_file_multi_part_form_data_sns(self):
        filename = 'sns-uploaded-example-2.csv'
        self.t_patch_mp(self.sns_uploads_folder, filename, self.sns_upload)
        self.check_copied_sns_file_exists(filename)


    def test_GA_patch_multiple_files_multi_part_form_data(self):
        newfilename1 = 'n3'
        newfilename2 = 'n4'
        try:
            os.remove(os.path.normpath(self.uploads_folder + '/' + newfilename1))
            os.remove(os.path.normpath(self.uploads_folder + '/' + newfilename2))
        except OSError:
            pass
        files = [('file', (newfilename1, open(self.example_csv, 'rb'), 'text/html')),
                 ('file', (newfilename2, open(self.example_csv, 'rb'), 'text/html'))]
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.post(self.upload, files=files, headers=headers)
        self.assertEqual(resp.status_code, 201)
        uploaded_file1 = os.path.normpath(self.uploads_folder + '/' + newfilename1)
        uploaded_file2 = os.path.normpath(self.uploads_folder + '/' + newfilename2)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file1))
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file2))
        files = [('file', (newfilename1, open(self.example_csv, 'rb'), 'text/html')),
                 ('file', (newfilename2, open(self.example_csv, 'rb'), 'text/html'))]
        resp2 = requests.patch(self.upload, files=files, headers=headers)
        self.assertEqual(resp2.status_code, 201)
        self.assertNotEqual(md5sum(self.example_csv), md5sum(uploaded_file1))
        self.assertNotEqual(md5sum(self.example_csv), md5sum(uploaded_file2))


    def t_put_mp(self, uploads_folder, newfilename, url):
        target = os.path.normpath(uploads_folder + '/' + newfilename)
        # remove file from previous round
        self.remove(uploads_folder, newfilename)
        # req1
        resp = self.mp_fd(newfilename, target, url, 'PUT')
        uploaded_file = os.path.normpath(uploads_folder + '/' + newfilename)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file))
        # req2
        resp2 = self.mp_fd(newfilename, target, url, 'PUT')
        self.assertEqual(resp2.status_code, 201)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file))


    def test_H_put_file_multi_part_form_data(self):
        self.t_put_mp(self.uploads_folder, 'uploaded-example-3.csv', self.upload)


    def test_H1_put_file_multi_part_form_data_sns(self):
        filename = 'sns-uploaded-example-3.csv'
        self.t_put_mp(self.sns_uploads_folder, filename, self.sns_upload)
        self.check_copied_sns_file_exists(filename)


    def test_HA_put_multiple_files_multi_part_form_data(self):
        newfilename1 = 'n5'
        newfilename2 = 'n6'
        files = [('file', (newfilename1, open(self.example_csv, 'rb'), 'text/html')),
                 ('file', (newfilename2, open(self.example_csv, 'rb'), 'text/html'))]
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.put(self.upload, files=files, headers=headers)
        self.assertEqual(resp.status_code, 201)
        uploaded_file1 = os.path.normpath(self.uploads_folder + '/' + newfilename1)
        uploaded_file2 = os.path.normpath(self.uploads_folder + '/' + newfilename2)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file1))
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file2))
        files = [('file', (newfilename1, open(self.example_csv, 'rb'), 'text/html')),
                 ('file', (newfilename2, open(self.example_csv, 'rb'), 'text/html'))]
        resp2 = requests.put(self.upload, files=files, headers=headers)
        self.assertEqual(resp2.status_code, 201)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file1))
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file2))


    def test_H4XX_when_no_keydir_exists(self):
        newfilename = 'new1'
        target = os.path.normpath(self.sns_uploads_folder + '/' + newfilename)
        resp1 = self.mp_fd(newfilename, target, self.upload_sns_wrong, 'PUT')
        resp2 = self.mp_fd(newfilename, target, self.upload_sns_wrong, 'POST')
        resp3 = self.mp_fd(newfilename, target, self.upload_sns_wrong, 'PATCH')
        self.assertEqual([resp1.status_code, resp2.status_code, resp3.status_code], [400, 400, 400])


    # streaming endpoint

    def test_I_put_file_to_streaming_endpoint_no_chunked_encoding_data_binary(self):
        newfilename = 'streamed-not-chunked'
        uploaded_file = os.path.normpath(self.uploads_folder + '/' + self.test_group + '/' + newfilename)
        try:
            os.remove(uploaded_file)
        except OSError:
            pass
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'], 'Filename': newfilename}
        resp = requests.put(self.stream, data=open(self.example_csv), headers=headers)
        self.assertEqual(resp.status_code, 201)

        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file))


    def test_K_put_stream_file_chunked_transfer_encoding(self):
        newfilename = 'streamed-put-example.csv'
        uploaded_file = os.path.normpath(self.uploads_folder + '/' + self.test_group + '/' + newfilename)
        try:
            os.remove(uploaded_file)
        except OSError:
            pass
        headers = {'Filename': 'streamed-put-example.csv',
                   'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Expect': '100-Continue'}
        resp = requests.put(self.stream, data=lazy_file_reader(self.example_csv), headers=headers)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file))
        resp = requests.put(self.stream, data=lazy_file_reader(self.example_csv), headers=headers)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file))
        self.assertEqual(resp.status_code, 201)


    # Informational
    #--------------

    def test_N_head_on_uploads_fails_when_it_should(self):
        resp1 = requests.head(self.upload)
        resp2 = requests.head(self.upload,
                              headers={'Authorization': 'Bearer ' + TEST_TOKENS['VALID']})
        self.assertEqual(resp1.status_code, 401)
        self.assertEqual(resp2.status_code, 400)


    def test_O_head_on_uploads_succeeds_when_conditions_are_met(self):
        files = {'file': ('example.csv', open(self.example_csv))}
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.head(self.upload, headers=headers, files=files)
        self.assertEqual(resp.status_code, 201)


    def test_P_head_on_stream_fails_when_it_should(self):
        pass


    def test_Q_head_on_stream_succeeds_when_conditions_are_met(self):
        pass

    # Support OPTIONS

    # Space issues

    def test_R_report_informative_error_when_running_out_space(self):
        pass
        # [Errno 28] No space left on device

    # https://auth0.com/blog/critical-vulnerabilities-in-json-web-token-libraries/
    # make sure alg : none JWT rejected
    # make sure cannot select any other alg

    # JSON data (from nettskjema)
    #----------------------------

    def test_S_create_table(self):
        table_def = self.example_codebook
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.post(self.base_url + '/storage/rpc/create_table',
                             data=json.dumps(table_def), headers=headers)
        self.assertEqual(resp.status_code, 201)


    def test_T_create_table_is_idempotent(self):
        table_def = self.example_codebook
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.post(self.base_url + '/storage/rpc/create_table',
                             data=json.dumps(table_def), headers=headers)
        self.assertEqual(resp.status_code, 201)


    def test_U_add_column_codebook(self):
        table_def = self.example_codebook
        table_def['definition']['pages'][0]['elements'].append({
            'elementType': 'QUESTION',
            'questions': [{'externalQuestionId': 'var3'}]})
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.post(self.base_url + '/storage/rpc/create_table',
                             data=json.dumps(table_def), headers=headers)
        self.assertEqual(resp.status_code, 201)


    def test_V_post_data(self):
        data = {'submission_id':1, 'age':93}
        bulk_data = [{'submission_id':4, 'var1':'something', 'var2':'nothing'},
                     {'submission_id':3, 'var1':'sensitive', 'var2': 'kablamo'}]
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp1 = requests.post(self.base_url + '/storage/form_63332',
                              data=json.dumps(data), headers=headers)
        resp2 = requests.post(self.base_url + '/storage/form_63332',
                              data=json.dumps(bulk_data), headers=headers)
        self.assertEqual(resp1.status_code, 201)
        self.assertEqual(resp2.status_code, 201)


    def test_W_create_table_generic(self):
        table_def = {'table_name': 'test1',
                     'columns': [{'name': 'x', 'type': 'int', 'constraints': {'not_null': True}},
                                 {'name': 'y', 'type': 'text'}]}
        data = {'type': 'generic', 'definition': table_def}
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.post(self.base_url + '/storage/rpc/create_table',
                             data=json.dumps(data), headers=headers)
        self.assertEqual(resp.status_code, 201)


    def test_X_post_encrypted_data(self):
        encrypted_data_ns = build_payload(self.config, 'ns')
        encrypted_data_gen = build_payload(self.config, 'generic')
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp1 = requests.post(self.base_url + '/storage/encrypted_data',
                             data=json.dumps(encrypted_data_ns), headers=headers)
        resp2 = requests.post(self.base_url + '/storage/encrypted_data',
                             data=json.dumps(encrypted_data_gen), headers=headers)
        self.assertEqual(resp1.status_code, 201)
        self.assertEqual(resp2.status_code, 201)


    # More Authn+z
    # ------------

    def test_Y_invalid_project_number_rejected(self):
        data = {'submission_id':11, 'age':193}
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.post('http://localhost:' + str(self.config['port']) + '/p12-2193-1349213*&^/storage/form_63332',
                             data=json.dumps(data), headers=headers)
        self.assertEqual(resp.status_code, 401)


    def test_Z_token_for_other_project_rejected(self):
        data = {'submission_id':11, 'age':193}
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['WRONG_PROJECT']}
        resp = requests.post(self.base_url + '/storage/form_63332',
                             data=json.dumps(data), headers=headers)
        self.assertEqual(resp.status_code, 401)


    # Handling custom content-types, on-the-fly
    # -----------------------------------------

    # Directories:
    # -----------
    # tar           -> untar
    # tar.gz        -> decompress, untar
    # tar.aes       -> decrypt, untar
    # tar.gz.aes    -> decrypt, uncompress, untar

    # Files:
    # -----
    # aes           -> decrypt
    # gz            -> uncompress
    # gz.aes        -> decrypt, uncompress

    def test_Za_stream_tar_without_custom_content_type_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Filename': 'example.tar'}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_tar),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        # checksum comparison

    def test_Zb_stream_tar_with_custom_content_type_untar_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/tar',
                   'Filename': 'totar'}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_tar),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        # check contents

    def test_Zc_stream_tar_gz_with_custom_content_type_untar_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/tar.gz',
                   'Filename': 'totar2'}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_tar_gz),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        # check contents

    def test_Zd_stream_aes_with_custom_content_type_decrypt_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/aes',
                   'Aes-Key': self.enc_symmetric_secret,
                   'Filename': 'decrypted-aes.csv'}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_aes),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        with open(self.uploads_folder + '/' + self.test_group + '/decrypted-aes.csv', 'r') as uploaded_file:
            self.assertEqual('x,y\n4,5\n2,1\n', uploaded_file.read())

    def test_Zd0_stream_aes_with_iv_and_custom_content_type_decrypt_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/aes',
                   'Aes-Key': self.enc_hex_aes_key,
                   'Aes-Iv': self.hex_aes_iv,
                   'Filename': 'decrypted-aes2.csv'}
        resp1 = requests.put(self.stream,
                             data=lazy_file_reader(self.example_aes_with_key_and_iv),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        with open(self.uploads_folder + '/' + self.test_group + '/decrypted-aes2.csv', 'r') as uploaded_file:
            self.assertEqual('x,y\n4,5\n2,1\n', uploaded_file.read())

    def test_Zd1_stream_binary_aes_with_iv_and_custom_content_type_decrypt_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/aes-octet-stream',
                   'Aes-Key': self.enc_hex_aes_key,
                   'Aes-Iv': self.hex_aes_iv,
                   'Filename': 'decrypted-binary-aes.csv'}
        resp1 = requests.put(self.stream,
                             data=lazy_file_reader(self.example_binary_aes_with_key_and_iv),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        with open(self.uploads_folder + '/' + self.test_group + '/decrypted-binary-aes.csv', 'r') as uploaded_file:
            self.assertEqual('x,y\n4,5\n2,1\n', uploaded_file.read())

    def test_Ze_stream_tar_aes_with_custom_content_type_decrypt_untar_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/tar.aes',
                   'Aes-Key': self.enc_symmetric_secret,
                   'Filename': 'totar3'}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_tar_aes),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        # check contents

    def test_Ze0_stream_tar_aes_with_iv_and_custom_content_type_decrypt_untar_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/tar.aes',
                   'Aes-Key': self.enc_hex_aes_key,
                   'Aes-Iv': self.hex_aes_iv,
                   'Filename': 'totar'}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_tar_aes_with_key_and_iv),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        # check contents

    def test_Zf_stream_tar_aes_with_custom_content_type_decrypt_untar_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/tar.gz.aes',
                   'Aes-Key': self.enc_symmetric_secret,
                   'Filename': 'totar4'}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_tar_gz_aes),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        # check contents

    def test_Zf0_stream_tar_aes_with_iv_and_custom_content_type_decrypt_untar_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/tar.gz.aes',
                   'Aes-Key': self.enc_hex_aes_key,
                   'Aes-Iv': self.hex_aes_iv,
                   'Filename': 'totar2'}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_tar_gz_aes_with_key_and_iv),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        # check contents

    def test_Zg_stream_gz_with_custom_header_decompress_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/gz',
                   'Filename': 'ungz1'}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_gz),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        with open(self.uploads_folder + '/' + self.test_group + '/ungz1', 'r') as uploaded_file:
           self.assertEqual('x,y\n4,5\n2,1\n', uploaded_file.read())


    def test_Zh_stream_gz_with_custom_header_decompress_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/gz.aes',
                   'Filename': 'ungz-aes1',
                   'Aes-Key': self.enc_symmetric_secret}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_gz_aes),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)
        # This ought to work, but does not
        with open(self.uploads_folder + '/' + self.test_group + '/ungz-aes1', 'r') as uploaded_file:
            self.assertEqual('x,y\n4,5\n2,1\n', uploaded_file.read())


    def test_Zh0_stream_gz_with_iv_and_custom_header_decompress_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Content-Type': 'application/gz.aes',
                   'Filename': 'ungz-aes1',
                   'Aes-Key': self.enc_hex_aes_key,
                   'Aes-Iv': self.hex_aes_iv}
        resp1 = requests.put(self.stream, data=lazy_file_reader(self.example_gz_aes_with_key_and_iv),
                             headers=headers)
        self.assertEqual(resp1.status_code, 201)

    def test_ZA_choosing_file_upload_directories_based_on_pnum_works(self):
        newfilename = 'uploaded-example-p12.csv'
        try:
            os.remove(os.path.normpath(self.uploads_folder_p12 + '/' + newfilename))
        except OSError:
            pass
        headers = {'Authorization': 'Bearer ' + P12_TOKEN}
        files = {'file': (newfilename, open(self.example_csv))}
        # remove hard-coded port from this, and similar tests
        resp1 = requests.post('http://localhost:' + str(self.config['port']) + '/p12/files/upload', files=files, headers=headers)
        self.assertEqual(resp1.status_code, 201)
        uploaded_file = os.path.normpath(self.uploads_folder_p12 + '/' + newfilename)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file))
        newfilename2 = 'streamed-put-example-p12.csv'
        try:
            os.remove(os.path.normpath(self.uploads_folder_p12 + '/p12-member-group/' + newfilename2))
        except OSError:
            pass
        headers2 = {'Filename': 'streamed-put-example-p12.csv',
                   'Authorization': 'Bearer ' + P12_TOKEN,
                   'Expect': '100-Continue'}
        resp2 = requests.put('http://localhost:' + str(self.config['port']) + '/p12/files/stream',
                            data=lazy_file_reader(self.example_csv), headers=headers2)
        self.assertEqual(resp2.status_code, 201)
        uploaded_file2 = os.path.normpath(self.uploads_folder_p12 + '/p12-member-group/' + newfilename2)
        self.assertEqual(md5sum(self.example_csv), md5sum(uploaded_file2))

    def test_ZB_sns_folder_logic_is_correct(self):
        # non-existent project
        self.assertRaises(Exception, project_sns_dir, '/tsd/pXX/data/durable',
                         'p1000', '255CE5ED50A7558B', '98765')
        # lowercase in key id
        self.assertRaises(Exception, project_sns_dir, '/tsd/pXX/data/durable',
                         'p11', '255cE5ED50A7558B', '98765')
        # too long but still valid key id
        self.assertRaises(Exception, project_sns_dir, '/tsd/pXX/data/durable',
                         'p11', '255CE5ED50A7558BXIJIJ87878', '98765')
        # non-numeric formid
        self.assertRaises(Exception, project_sns_dir, '/tsd/pXX/data/durable',
                         'p11', '255CE5ED50A7558B', '99999-%$%&*')
        # note: /tsd/p11/data/durable _must_ exist for this test to pass
        self.assertEqual(project_sns_dir('/tsd/pXX/data/durable', 'p11', '255CE5ED50A7558B', '98765'),
                         '/tsd/p11/data/durable/nettskjema-submissions/255CE5ED50A7558B/98765')
        try:
            os.rmdir('/tsd/p11/data/durable/nettskjema-submissions/255CE5ED50A7558B/98765')
            os.rmdir('/tsd/p11/data/durable/nettskjema-submissions/255CE5ED50A7558B')
        except OSError:
            pass

    def test_ZC_setting_ownership_based_on_user_works(self):
        token = gen_test_token_for_user(self.config, self.test_user)
        headers = {'Authorization': 'Bearer ' + token,
                   'Filename': 'testing-chowner.txt'}
        resp = requests.put(self.stream,
                            data=lazy_file_reader(self.example_gz_aes),
                            headers=headers)
        intended_owner = pwd.getpwnam(self.test_user).pw_uid
        effective_owner = os.stat(self.uploads_folder + '/' + self.test_group + '/testing-chowner.txt').st_uid
        self.assertEqual(intended_owner, effective_owner)

    def test_ZD_cannot_upload_empty_file_to_sns(self):
        files = {'file': ('an-empty-file', open(self.an_empty_file))}
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID']}
        resp = requests.put(self.sns_upload,
                            files=files,
                            headers=headers)
        self.assertEqual(resp.status_code, 400)

    # client-side specification of groups

    def test_ZE_stream_works_with_client_specified_group(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Expect': '100-Continue'}
        url = self.stream + '/streamed-example-with-group-spec.csv?group=p11-member-group'
        resp = requests.post(url,
                             data=lazy_file_reader(self.example_csv),
                             headers=headers)
        self.assertEqual(resp.status_code, 201)

    def test_ZF_stream_does_not_work_with_client_specified_group_wrong_pnum(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Expect': '100-Continue'}
        url = self.stream + '/streamed-example-with-group-spec.csv?group=p12-member-group'
        resp = requests.post(url,
                             data=lazy_file_reader(self.example_csv),
                             headers=headers)
        self.assertEqual(resp.status_code, 401)

    def test_ZG_stream_does_not_work_with_client_specified_group_nonsense_input(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Expect': '100-Continue'}
        url = self.stream + '/streamed-example-with-group-spec.csv?group=%2Fusr%2Fbin%2Fecho%20%24PATH'
        resp = requests.post(url,
                             data=lazy_file_reader(self.example_csv),
                             headers=headers)
        self.assertEqual(resp.status_code, 401)

    def test_ZH_stream_does_not_work_with_client_specified_group_not_a_member(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['VALID'],
                   'Expect': '100-Continue'}
        url = self.stream + '/streamed-example-with-group-spec.csv?group=p11-data-group'
        resp = requests.post(url,
                             data=lazy_file_reader(self.example_csv),
                             headers=headers)
        self.assertEqual(resp.status_code, 401)

    # export

    def test_ZI_export_endpoints_require_auth(self):
        import_token = TEST_TOKENS['VALID']
        resp = requests.get(self.export)
        self.assertEqual(resp.status_code, 401)
        resp = requests.get(self.export + '/file1')
        self.assertEqual(resp.status_code, 401)
        resp = requests.get(self.export,
                            headers={'Authorization': 'Bearer ' + import_token})
        self.assertEqual(resp.status_code, 401)
        resp = requests.get(self.export + '/file1',
                            headers={'Authorization': 'Bearer ' + import_token})
        self.assertEqual(resp.status_code, 401)


    def test_ZJ_export_file_restrictions_enforced(self):
        headers={'Authorization': 'Bearer ' + TEST_TOKENS['EXPORT']}
        for name in ['/bin/bash -c', '!#/bin/bash', '~!@#$%^&*()-+', '../../../p01/data/durable']:
            resp = requests.get(self.export + '/' + name, headers=headers)
            self.assertEqual(resp.status_code, 403)


    def test_ZK_export_list_dir_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['EXPORT']}
        resp = requests.get(self.export, headers=headers)
        data = json.loads(resp.text)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(len(data['files']), 2)
        self.assertTrue(len(data['files'][0].keys()), 3) # seems broken


    def test_ZL_export_file_works(self):
        headers = {'Authorization': 'Bearer ' + TEST_TOKENS['ADMIN']}
        resp = requests.get(self.export + '/file1', headers=headers)
        self.assertEqual(resp.text, u'some data\n')
        self.assertEqual(resp.status_code, 200)


    # resumable uploads


    def create_simulated_resumable_in_upload_dir(self, filepath, filename, chunksize, bad_data=False):
        resume_dir = self.uploads_folder + '/' + self.test_upload_id
        try:
            os.makedirs(resume_dir)
        except OSError:
            pass # it already exists, we do not care
        chunk1 = ''.join([resume_dir, '/', filename, '.chunk.1'])
        with open(chunk1, 'wb') as fout:
            with open(filepath, 'r') as fin:
                if not bad_data:
                    chunk_data = fin.read(chunksize)
                else:
                    chunk_data = 'wrong'
                fout.write(chunk_data)


    def start_new_resumable(self, filepath, chunksize=1, large_file=False):
        token = TEST_TOKENS['VALID']
        filename = os.path.basename(filepath)
        url = '%s/%s' % (self.stream, filename)
        resp = fileapi.initiate_resumable('', self.test_project, filepath,
                                          token, chunksize=chunksize, new=True, group=None,
                                          verify=False, dev_url=url)
        self.assertEqual(resp['max_chunk'], u'end')
        self.assertTrue(resp['id'] is not None)
        self.assertEqual(resp['filename'], filename)
        if not large_file:
            self.assertEqual(md5sum(filepath),
                md5sum(self.uploads_folder + '/' + self.test_group + '/' + filename))


    def test_ZM_resume_new_upload_works_is_idempotent(self):
        self.start_new_resumable(self.resume_file1, chunksize=5)


    def do_resume(self, by_id=False, verify=False, bad_data=False):
        token = TEST_TOKENS['VALID']
        chunksize = 5
        filename = os.path.basename(self.resume_file2)
        self.create_simulated_resumable_in_upload_dir(self.resume_file2, filename,
                                                      chunksize, bad_data)
        url = '%s/%s' % (self.resumables, filename)
        if by_id:
            upload_id = self.test_upload_id
        else:
            upload_id = None
        resp = fileapi.initiate_resumable('', self.test_project, self.resume_file2,
                                          token, chunksize=5, new=False, group=None,
                                          verify=verify, upload_id=upload_id, dev_url=url)
        if bad_data:
            self.assertEqual(resp, None)
        else:
            self.assertEqual(resp['max_chunk'], u'end')
            self.assertTrue(resp['id'] is not None)
            self.assertEqual(resp['filename'], filename)
            self.assertEqual(md5sum(self.resume_file2),
                md5sum(self.uploads_folder + '/' + self.test_group + '/' + filename))
        try:
            shutil.rmtree(self.uploads_folder + '/' + self.test_upload_id)
        except OSError:
            pass

    def test_ZN_resume_works_with_upload_id_match(self):
        self.do_resume(by_id=True, verify=True)


    def test_ZO_resume_works_with_filename_match(self):
        self.do_resume(by_id=False, verify=True)


    def test_ZP_resume_start_new_upload_if_md5_mismatch(self):
        self.do_resume(by_id=False, verify=True, bad_data=True)


    def test_ZQ_large_file_resume(self):
        _100mb = 1000*1000*50 # for 1gb file
        self.start_new_resumable(self.large_file, chunksize=_100mb, large_file=True)


def main():
    runner = unittest.TextTestRunner()
    suite = []
    suite.append(unittest.TestSuite(map(TestFileApi, [
        # authz
        'test_A_mangled_valid_token_rejected',
        'test_B_invalid_signature_rejected',
        'test_C_token_with_wrong_role_rejected',
        'test_D_timed_out_token_rejected',
        'test_E_unauthenticated_request_rejected',
        # form-data
        'test_F_post_file_multi_part_form_data',
        'test_F1_post_file_multi_part_form_data_sns',
        'test_FA_post_multiple_files_multi_part_form_data',
        'test_G_patch_file_multi_part_form_data',
        'test_G1_patch_file_multi_part_form_data_sns',
        'test_GA_patch_multiple_files_multi_part_form_data',
        'test_H_put_file_multi_part_form_data',
        'test_H1_put_file_multi_part_form_data_sns',
        'test_HA_put_multiple_files_multi_part_form_data',
        # sns
        'test_H4XX_when_no_keydir_exists',
        # streaming
        'test_I_put_file_to_streaming_endpoint_no_chunked_encoding_data_binary',
        'test_K_put_stream_file_chunked_transfer_encoding',
        # head
        'test_N_head_on_uploads_fails_when_it_should',
        'test_O_head_on_uploads_succeeds_when_conditions_are_met',
        # sqlite backend
        'test_S_create_table',
        'test_T_create_table_is_idempotent',
        'test_U_add_column_codebook',
        'test_V_post_data',
        'test_W_create_table_generic',
        'test_X_post_encrypted_data',
        # pnum logic
        'test_Y_invalid_project_number_rejected',
        'test_Z_token_for_other_project_rejected',
        # custom data processing
        'test_Za_stream_tar_without_custom_content_type_works',
        'test_Zb_stream_tar_with_custom_content_type_untar_works',
        'test_Zc_stream_tar_gz_with_custom_content_type_untar_works',
        'test_Zd_stream_aes_with_custom_content_type_decrypt_works',
        'test_Zd0_stream_aes_with_iv_and_custom_content_type_decrypt_works',
        'test_Zd1_stream_binary_aes_with_iv_and_custom_content_type_decrypt_works',
        'test_Ze_stream_tar_aes_with_custom_content_type_decrypt_untar_works',
        'test_Ze0_stream_tar_aes_with_iv_and_custom_content_type_decrypt_untar_works',
        'test_Zf_stream_tar_aes_with_custom_content_type_decrypt_untar_works',
        'test_Zf0_stream_tar_aes_with_iv_and_custom_content_type_decrypt_untar_works',
        'test_Zg_stream_gz_with_custom_header_decompress_works',
        'test_Zh_stream_gz_with_custom_header_decompress_works',
        'test_Zh0_stream_gz_with_iv_and_custom_header_decompress_works',
        # upload dirs
        'test_ZA_choosing_file_upload_directories_based_on_pnum_works',
        'test_ZB_sns_folder_logic_is_correct',
        'test_ZC_setting_ownership_based_on_user_works',
        'test_ZD_cannot_upload_empty_file_to_sns',
        # groups
        'test_ZE_stream_works_with_client_specified_group',
        'test_ZF_stream_does_not_work_with_client_specified_group_wrong_pnum',
        'test_ZG_stream_does_not_work_with_client_specified_group_nonsense_input',
        'test_ZH_stream_does_not_work_with_client_specified_group_not_a_member',
        # export
        'test_ZI_export_endpoints_require_auth',
        'test_ZJ_export_file_restrictions_enforced',
        'test_ZK_export_list_dir_works',
        'test_ZL_export_file_works',
        # resume
        'test_ZM_resume_new_upload_works_is_idempotent',
        'test_ZN_resume_works_with_upload_id_match',
        'test_ZO_resume_works_with_filename_match',
        'test_ZP_resume_start_new_upload_if_md5_mismatch',
        #'test_ZQ_large_file_resume',
        ])))
    map(runner.run, suite)


if __name__ == '__main__':
    main()
