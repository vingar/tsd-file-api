
""" API to support file uploads into TSD projects via the proxy. """

import sys
import jwt # https://github.com/davedoesdev/python-jwt
import os
import yaml
import psycopg2
import psycopg2.pool
import time
from flask import Flask, request, redirect, url_for, jsonify, g
from werkzeug.utils import secure_filename
from flask import send_from_directory
from werkzeug.formparser import FormDataParser

# add method for handling PGP encrypted files
FormDataParser.parse_functions['multipart/encrypted'] = FormDataParser._parse_multipart

def read_config(file):
    with open(file) as f:
        conf = yaml.load(f)
    return conf


CONF = read_config(sys.argv[1])
UPLOAD_FOLDER = CONF['file_uploads']
JWT_SECRET = CONF['jwt_secret']
JWT_MAX_AGE = 60*60
ALLOWED_EXTENSIONS = set(['txt', 'pdf', 'png', 'jpg', 'jpeg', 'csv', 'tsv', 'asc'])
MINCONN = 4
MAXCONN = 10
pool = psycopg2.pool.SimpleConnectionPool(MINCONN, MAXCONN, \
    host=CONF['host'], database=CONF['db'], user=CONF['user'], password=CONF['pw'])
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 40 * 1024 * 1024


def get_dbconn():
    dbconn = getattr(g, 'dbconn', None)
    if dbconn is None:
        conn = pool.getconn()
        dbconn = g.dbconn = conn
    return dbconn


@app.teardown_appcontext
def close_connection(exception):
    dbconn = getattr(g, 'dbconn', None)
    if dbconn is not None:
        dbconn.close()


@app.route('/upload_signup', methods=['GET', 'POST'])
def upload_signup():
    """Create a user, password entry that could allow a user to _upload_ files.
    This user must first be verified by a TSD admin before being allowed to request
    access tokens.
    """
    data = request.get_json()
    get_dbconn()
    cur = g.dbconn.cursor()
    cur.execute("select public.signup(%s, %s)", (data['email'], data['pass']))


@app.route('/download_signup', methods=['GET', 'POST'])
def download_signup():
    """Create a user, password entry that could allow a user to _download_ files.
    This user must first be verified by a TSD admin before being allowed to request
    access tokens. This is a much more stringent authentication process involving SAML
    integration with id-porten.
    """
    data = request.get_json()
    get_dbconn()
    cur = g.dbconn.cursor()
    cur.execute("select reports.signup(%s, %s)", (data['external_user_id'], data['user_group']))


@app.route('/upload_token', methods=['GET', 'POST'])
def get_upload_token():
    """Get a JWT that allows _uploading_ files. These tokens are the same as those
    generated by the storage API. The implication is that if you have permission
    to upload files then you have permission to upload json data and vice versa.
    If the storage/retrieval APIs are deployed in the TSD project then these
    app routes are not strictly necessary, but having them enables the deployment
    of the file-api as a standalone component (along with the postgresql db).
    """
    data = request.get_json()
    get_dbconn()
    cur = g.dbconn.cursor()
    cur.execute("select public.token(%s, %s)", (data['email'], data['pass']))
    res = cur.fetchall()
    token = res[0][0]
    return jsonify([{ 'token': token }])


@app.route('/download_token', methods=['GET', 'POST'])
def get_download_token(saml_data):
    """Get a JWT that allows _downloading_ files.These tokens are the same as those
    generated by the retrieval API. The implication is that if you have permission
    to download files then you have permission to download json data and vice versa.
    If the storage/retrieval APIs are deployed in the TSD project then these
    app routes are not strictly necessary, but having them enables the deployment
    of the file-api as a standalone component (along with the postgresql db).
    """
    data = request.get_json()
    get_dbconn()
    cur = g.dbconn.cursor()
    cur.execute("select reports.token(%s)", (data['saml_data']))
    res = cur.fetchall()
    token = res[0][0]
    return jsonify([{ 'token': token }])


def verify_json_web_token(request_headers, required_role=None):
    """Verifies the authenticity of API credentials, as stored in a JSON Web Token
    (see jwt.io for more).

    Details:
    0) Checks for the existence of a token
    1) Checks the cryptographic integrity of the token - that it was obtained from an
    authoritative source with access to the secret key
    2) Extracts the JWT header and the claims
    3) Checks that the role assigned to the user in the db is allowed to perform the action
    4) Checks that the token has not expired - 1 hour is the current lifetime
    """
    try:
        token = request.headers['Authorization'].replace('Bearer ', '')
        header, claims = jwt.verify_jwt(token, JWT_SECRET, ['HS256'], checks_optional=True)
    except KeyError:
        return jsonify({'message': 'No JWT provided.'}), 400
    except jwt.jws.SignatureError:
        return jsonify({'message': 'Access forbidden - Unable to verify signature.'}), 403
    if claims['role'] != required_role:
        return jsonify({'message': 'Access forbidden - Your role does not allow this operation.'}), 403
    cutoff_time = int(time.time()) + JWT_MAX_AGE
    if int(claims['exp']) > cutoff_time:
        return jsonify({'message': 'Access forbidden - JWT expired.'}), 403
    else:
        return True


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1] in ALLOWED_EXTENSIONS


@app.route('/upload', methods=['POST'])
def upload_file():
    """Allows authenticated and authorized users to upload a file. Current max size is 40MB.
    All files are saved to the same directory.

    Content-Types:
    - For plain text ->             'Content-Type: multipart/form-data'
    - For PGP encrypted text use -> 'Content-Type: multipart/encrypted; protocol="pgp-encrypted"'

    Initiating actions after file uploads (not implemented yet):
    - request.mimetype - this is e.g. multipart/encrypted
    - request.mimetype_params - includes protocol, boundary
    - after saving file, if mimetype multipart/encrypted
        - do something with it, like decrypt it
        - perhaps only if we also get another custom header, like e.g. X-Decrypt
    """
    status = verify_json_web_token(request.headers, required_role='app_user')
    if status is not True:
        return status
    if request.method == 'POST':
        if 'file' not in request.files:
            return jsonify({'message': 'file not found'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'message': 'no filename specified'}), 400
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            return jsonify({'message': 'uploaded file'}), 201
        else:
            return jsonify({'message': 'file type not allowed'}), 400


def list_files():
    pass


@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    """Allows authenticated and authorized users to download a file.
    """
    status = verify_json_web_token(request.headers, required_role='full_access_reports_user')
    if status is not True:
        return status
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# should not have debug in prod
if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
