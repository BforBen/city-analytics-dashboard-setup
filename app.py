import sys, json

from urllib import urlencode
from tarfile import TarFile
from gzip import GzipFile
from StringIO import StringIO
from uuid import uuid4
from time import sleep
from tempfile import mkdtemp
from os.path import commonprefix, join, isdir, exists, basename
from shutil import make_archive, rmtree
from os import environ

import logging
from logging.handlers import SMTPHandler

from flask import Flask, request, redirect, render_template, jsonify, send_file, make_response
from requests import get, post, Session
from flask_heroku import Heroku
import oauth2, psycopg2

import builders

display_screen_tarball_url = 'https://github.com/BforBen/city-analytics-dashboard/tarball/master/'

google_authorize_url = 'https://accounts.google.com/o/oauth2/auth'
google_access_token_url = 'https://accounts.google.com/o/oauth2/token'

# Use URL from http://stackoverflow.com/questions/22127982/what-is-the-difference-of-internalwebpropertyid-defaultprofileid-and-profileid
google_analytics_properties_url = 'https://www.googleapis.com/analytics/v3/management/accounts/~all/webproperties/~all/profiles'
google_plus_whoami_url = 'https://www.googleapis.com/plus/v1/people/me'
google_auth_scopes = 'email', 'https://www.googleapis.com/auth/analytics', 'https://www.googleapis.com/auth/analytics.readonly'

heroku_authorize_url = 'https://id.heroku.com/oauth/authorize'
heroku_access_token_url = 'https://id.heroku.com/oauth/token'

class SetupError (Exception):
    pass

app = Flask(__name__)
heroku = Heroku(app)

app.config['SEND_EMAIL'] = environ['SEND_EMAIL']
app.config['EMAIL_RECIPIENT'] = environ['EMAIL_RECIPIENT']
app.config['EMAIL_SENDER'] = environ['EMAIL_SENDER']

app.config['SQLALCHEMY_DATABASE_URI'] = environ['DATABASE_URL']

with psycopg2.connect(app.config['SQLALCHEMY_DATABASE_URI']) as connection:
        with connection.cursor() as cursor:
            cursor.execute(open("schema.pgsql", "r").read())

logger = logging.getLogger('noteworthy')
logger.setLevel(logging.DEBUG)

handler1 = logging.StreamHandler(sys.stderr)
handler1.setLevel(logging.DEBUG)
logger.addHandler(handler1)

if 'SMTP_USERNAME' in environ and 'SMTP_PASSWORD' in environ:
    app.config['SMTP_USERNAME'] = environ['SMTP_USERNAME']
    app.config['SMTP_PASSWORD'] = environ['SMTP_PASSWORD']
    app.config['SMTP_HOSTNAME'] = environ['SMTP_HOSTNAME']
    app.config['SEND_EMAIL'] = True
    
    handler2 = SMTPHandler(app.config['SMTP_HOSTNAME'], app.config['EMAIL_SENDER'],
                           (app.config['EMAIL_RECIPIENT']),
                           'City Analytics Dashboard error report',
                           (app.config['SMTP_USERNAME'], app.config['SMTP_PASSWORD']))

    handler2.setLevel(logging.WARNING)
    logger.addHandler(handler2)

@app.errorhandler(builders.SetupError)
def on_setuperror(error):
    '''
    '''
    logger.error('City Analytics Dashboard - Heroku error', exc_info=True)
    values = dict(message=error.message)
    return make_response(render_template('error.html', **values), 400)

@app.route("/")
def index():
    ''' Render front page with all the info.
    
        If not running locally, force SSL.
    '''
    scheme, host = get_scheme(request), request.host
    
    if scheme == 'http' and host[:9] not in ('localhost', '127.0.0.1','city-analytics-dashboard-setup-bforben.c9users.io'):
        return redirect('https://lganalytics-dashboard-setup.herokuapp.com')
    
    logger.debug('GET / {}')

    return render_template('index.html')

@app.route('/authorize-google', methods=['POST'])
def authorize_google():
    ''' Ask Google to authenticate. On success, return to /callback-google.
    '''
    client_id, client_secret, redirect_uri = google_client_info(request)

    query_string = urlencode(dict(client_id=client_id, redirect_uri=redirect_uri,
                                  scope=' '.join(google_auth_scopes),
                                  state=str(uuid4()), response_type='code',
                                  access_type='offline', approval_prompt='force'))
    
    logger.debug('POST /authorize-google redirect {}?{}'.format(google_authorize_url, query_string))

    return redirect(google_authorize_url + '?' + query_string)

@app.route('/callback-google')
def callback_google():
    ''' Complete Google authentication, get web properties, and show the form.
    '''
    code, state = request.args.get('code'), request.args.get('state')
    client_id, client_secret, redirect_uri = google_client_info(request)
    
    data = dict(client_id=client_id, client_secret=client_secret,
                code=code, redirect_uri=redirect_uri,
                grant_type='authorization_code')
    
    logger.debug('GET /callback-google {}'.format(data))

    try:
        access = get_google_access_token(data)
        access_token, refresh_token = access['access_token'], access['refresh_token']
    
        name, email = get_google_personal_info(access_token)
        properties = get_google_analytics_properties(access_token)
    
        if not properties:
            raise SetupError("Your Google Account isn't associated with any Google Analytics properties. Log in to Google with a different account?")
    
    except SetupError, e:
        logger.error('City Analytics Dashboard - Google error', exc_info=True)
        values = dict(message=e.message)
        return make_response(render_template('error.html', **values), 400)
    
    values = dict(client_id=client_id, client_secret=client_secret,
                  refresh_token=refresh_token, properties=properties,
                  name=name, email=email)
    
    logger.debug('GET /callback-google {}'.format(values))

    return render_template('index.html', **values)

@app.route('/prepare-app', methods=['POST'])
def prepare_app():
    ''' Prepare app, ask Heroku to authenticate, return to /callback-heroku.
    '''
    view_id, website_url = request.form.get('property').split(' ', 1)
    name, email = request.form.get('name'), request.form.get('email')
    client_id = request.form.get('client_id')
    client_secret = request.form.get('client_secret')
    refresh_token = request.form.get('refresh_token')

    organisation_name = request.form.get('organisation_name', 'LG Analytics Dashboard')
    shortcut_icon = request.form.get('shortcut_icon', 'data:image/x-icon;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQEAYAAABPYyMiAAAABmJLR0T///////8JWPfcAAAACXBIWXMAAABIAAAASABGyWs+AAAAF0lEQVRIx2NgGAWjYBSMglEwCkbBSAcACBAAAeaR9cIAAAAASUVORK5CYII=')
    theme_colour = request.form.get('theme_colour', '#212121')
    title_filter = request.form.get('title_filter', ' | ')
    
    env = dict(LANG='en_US.UTF-8', RACK_ENV='production',
               GA_VIEW_ID=view_id, GA_WEBSITE_URL=website_url,
               CLIENT_ID=client_id, CLIENT_SECRET=client_secret,
               REFRESH_TOKEN=refresh_token,
               ORGANISATION_NAME=organisation_name,
               SHORTCUT_ICON=shortcut_icon, THEME_COLOUR=theme_colour,
               TITLE_FILTER=title_filter)
    
    tarpath = prepare_tarball(display_screen_tarball_url,
                              dict(name='Display Screen', env=env))
    
    logger.debug('GET /prepare-app {}'.format(app.config))
    
    with psycopg2.connect(app.config['SQLALCHEMY_DATABASE_URI']) as connection:
        with connection.cursor() as cursor:
            tarball_id = builders.add_connection(cursor, email, name, website_url, tarpath)
            builders.set_connection_datum(cursor, tarball_id, 'google name', name)
            builders.set_connection_datum(cursor, tarball_id, 'google email', email)
            builders.set_connection_datum(cursor, tarball_id, 'google url', website_url)
    
    client_id, _, redirect_uri = heroku_client_info(request)
    
    query_string = urlencode(dict(client_id=client_id, redirect_uri=redirect_uri,
                                  response_type='code', scope='global',
                                  state=str(tarball_id)))
    
    logger.debug('POST /prepare-app redirect {}?{}'.format(heroku_authorize_url, query_string))
    
    return redirect(heroku_authorize_url + '?' + query_string)

@app.route('/tarball/<int:tarball_id>')
def get_tarball(tarball_id):
    ''' Return the named application tarball from the temp directory.
    '''
    with psycopg2.connect(app.config['SQLALCHEMY_DATABASE_URI']) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT contents FROM tarballs WHERE id = %s',
                           (tarball_id, ))

            (tarball_data, ) = cursor.fetchone()
    
    filename = 'app-{0}.tar.gz'.format(tarball_id)
    return send_file(StringIO(tarball_data), as_attachment=True, attachment_filename=filename)

@app.route('/callback-heroku')
def callback_heroku():
    ''' Complete Heroku authentication, start app-setup, redirect to app page.
    '''
    code, tar_id = request.args.get('code'), request.args.get('state')
    client_id, client_secret, redirect_uri = heroku_client_info(request)

    try:
        data = dict(grant_type='authorization_code', code=code,
                    client_secret=client_secret, redirect_uri='')
    
        response = post(heroku_access_token_url, data=data)
        logger.debug('GET /callback-heroku {}'.format(response.json()))
        access = response.json()
    
        if response.status_code != 200:
            if 'message' in access:
                raise SetupError('Heroku says "{0}"'.format(access['message']))
            else:
                raise SetupError('Heroku Error')
    
        url = '{0}://{1}/tarball/{2}'.format(get_scheme(request), request.host, tar_id)
        setup_id, app_name = builders.create_app(builders.get_http_client(), access['access_token'], url)
        
        with psycopg2.connect(app.config['SQLALCHEMY_DATABASE_URI']) as connection:
            with connection.cursor() as cursor:
                builders.set_connection_datum(cursor, tar_id, 'app_name', app_name)
                builders.set_connection_datum(cursor, tar_id, 'app_setup_id', setup_id)
                builders.set_connection_datum(cursor, tar_id, 'access_token', access['access_token'])
                google_name = builders.get_connection_datum(cursor, tar_id, 'google name')
                google_email = builders.get_connection_datum(cursor, tar_id, 'google email')
                google_url = builders.get_connection_datum(cursor, tar_id, 'google url')
        
        try:
            if app.config['SEND_EMAIL']:
                fromaddr, toaddr = app.config['EMAIL_RECIPIENT']
                msg = 'From: {fromaddr}\r\nTo: {toaddr}\r\nCc: {fromaddr}\r\nSubject: City Analytics Dashboard got used\r\n\r\n{google_name} {google_email} at https://{app_name}.herokuapp.com for {google_url}.'.format(**locals())
                builders.send_email(fromaddr, toaddr, msg, app.config)
        except:
            logger.error('Local Gov Analytics Dashboard - SMTP error', exc_info=True)

        wait_url = '{0}://{1}/{2}/wait-for-heroku'.format(get_scheme(request), request.host, tar_id)
        return redirect(wait_url)
        
        return render_template('done.html',
                               settings_url=builders.heroku_app_activity_template.format(app_name),
                               application_url=builders.heroku_app_direct_template.format(app_name),
                               app_name=app_name)
    
        return redirect(builders.heroku_app_activity_template.format(app_name))
    
    except SetupError, e:
        logger.error('Local Gov Analytics Dashboard - Heroku error', exc_info=True)
        values = dict(message=e.message)
        return make_response(render_template('error.html', **values), 400)

@app.route('/<int:conn_id>/wait-for-heroku')
def wait_for_heroku(conn_id):
    '''
    '''
    with psycopg2.connect(app.config['SQLALCHEMY_DATABASE_URI']) as connection:
        with connection.cursor() as cursor:
            app_setup_id = builders.get_connection_datum(cursor, conn_id, 'app_setup_id')
            access_token = builders.get_connection_datum(cursor, conn_id, 'access_token')
    
    finished = builders.check_app(builders.get_http_client(), access_token, app_setup_id)
    
    if not finished:
        return render_template('wait.html',
                               url=request.path)
    
    finished_url = '{0}://{1}/{2}/finished'.format(get_scheme(request), request.host, conn_id)
    return redirect(finished_url)

@app.route('/<int:conn_id>/finished')
def heroku_complete(conn_id):
    '''
    '''
    with psycopg2.connect(app.config['SQLALCHEMY_DATABASE_URI']) as connection:
        with connection.cursor() as cursor:
            app_name = builders.get_connection_datum(cursor, conn_id, 'app_name')

    return render_template('done.html',
                           settings_url=builders.heroku_app_activity_template.format(app_name),
                           application_url=builders.heroku_app_direct_template.format(app_name),
                           app_name=app_name)

def get_scheme(request):
    ''' Get the current URL scheme, e.g. 'http' or 'https'.
    '''
    if 'x-forwarded-proto' in request.headers:
        return request.headers['x-forwarded-proto']
    
    return request.scheme

def get_google_access_token(data):
    ''' Get access token from Google API.
    '''
    response = post(google_access_token_url, data=data)
    access = response.json()

    logger.debug('get_google_access_token(): {} {}'.format(response.status_code, access))

    if response.status_code != 200:
        if 'error_description' in access:
            raise SetupError('Google says "{0}"'.format(access['error_description']))
        else:
            raise SetupError('Google Error')
    
    return access

def get_google_personal_info(access_token):
    ''' Get account name and email from Google Plus.
    '''
    response = get(google_plus_whoami_url, params={'access_token': access_token})
    whoami = response.json()
    
    if response.status_code != 200:
        if 'error_description' in whoami:
            raise SetupError('Google says "{0}"'.format(whoami['error_description']))
        else:
            raise SetupError('Google Error')
    
    emails = dict([(e['type'], e['value']) for e in whoami['emails']])
    email = emails.get('account', whoami['emails'][0]['value'])
    name = whoami['displayName']
    
    return name, email

def get_google_analytics_properties(access_token):
    ''' Get sorted list of web properties from Google Analytics.
    '''
    response = get(google_analytics_properties_url, params={'access_token': access_token})
    items = response.json()
    
    if response.status_code != 200:
        if 'error_description' in items:
            raise SetupError('Google says "{0}"'.format(items['error_description']))
        else:
            raise SetupError('Google Error')
    
    properties = [
        (item['id'], item['name'], item['websiteUrl'])
        for item in items['items']
        ]
    
    properties.sort(key=lambda p: p[1].lower())
    
    return properties
    
def google_client_info(request):
    ''' Return Client ID, secret, and redirect URI for Google OAuth use.
    '''
    scheme, host = get_scheme(request), request.host
    
    id, secret = environ['GOOGLE_ID'], environ['GOOGLE_SECRET']

    return id, secret, '{0}://{1}/callback-google'.format(scheme, host)

def heroku_client_info(request):
    ''' Return Client ID, secret, and redirect URI for Heroku OAuth use.
    '''
    scheme, host = get_scheme(request), request.host
    
    id, secret = environ['HEROKU_ID'], environ['HEROKU_SECRET']

    return id, secret, '{0}://{1}/callback-heroku'.format(scheme, host)

def prepare_tarball(url, app):
    ''' Prepare a tarball with app.json from the source URL.
    '''
    got = get(url, allow_redirects=True)
    raw = GzipFile(fileobj=StringIO(got.content))
    tar = TarFile(fileobj=raw)
    
    try:
        dirpath = mkdtemp(prefix='display-screen-')
        rootdir = join(dirpath, commonprefix(tar.getnames()))
        tar.extractall(dirpath)
        
        if not isdir(rootdir):
            raise Exception('"{0}" is not a directory'.format(rootdir))

        with open(join(rootdir, 'app.json'), 'w') as out:
            json.dump(app, out)
        
        tarpath = make_archive(dirpath, 'gztar', rootdir, '.')
        
    finally:
        rmtree(dirpath)
    
    return tarpath

if __name__ == '__main__':
    if sys.argv[-1] == 'ssl':
        context = ('ssl/server.crt', 'ssl/server.key')
    else:
        context = None

    app.run(host='0.0.0.0', port=5000, debug=True, ssl_context=context)
