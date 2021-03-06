from flask import Flask
from flask import render_template
from flask import jsonify, abort, send_file
from flask import request, Response, redirect, url_for, make_response
from flask.ext.login import LoginManager, current_user
from flask.ext.login import login_user, login_required, logout_user  
from platform_matcher import get_platform, get_release

from werkzeug.contrib.fixers import ProxyFix

from sqlalchemy import or_, and_

from database import DBSession
from forms import HostForm 
from forms import JumpHostForm
from forms import LoginForm
from forms import UserForm
from forms import RegionForm
from forms import ServerForm 
from forms import ScheduleInstallForm
from forms import HostScheduleInstallForm
from forms import AdminConsoleForm
from forms import SMTPForm
from forms import PreferencesForm
from forms import HostImportForm
from forms import DialogServerForm

from models import Host
from models import JumpHost
from models import InventoryJob
from models import ConnectionParam
from models import Log, logger 
from models import InventoryJobHistory
from models import InstallJob
from models import InstallJobHistory
from models import Region
from models import User
from models import Server
from models import SMTPServer
from models import SystemOption
from models import Package
from models import Preferences
from models import SMUMeta
from models import SMUInfo
from models import DownloadJob
from models import DownloadJobHistory
from models import get_download_job_key_dict

from validate import is_connection_valid
from validate import is_reachable

from constants import Platform
from constants import InstallAction
from constants import JobStatus
from constants import PackageState
from constants import ServerType
from constants import UserPrivilege
from constants import ConnectionType
from constants import BUG_SEARCH_URL
from constants import get_autlogs_directory, get_repository_directory, get_temp_directory

from filters import get_datetime_string
from filters import time_difference_UTC 
from filters import beautify_platform

from utils import get_datetime
from utils import get_file_list
from utils import make_url
from utils import trim_last_slash
from utils import is_empty
from utils import get_tarfile_file_list
from utils import comma_delimited_str_to_array

from server_helper import get_server_impl
from wtforms.validators import Required

from smu_utils import get_optimize_list
from smu_utils import get_missing_prerequisite_list
from smu_utils import get_download_info_dict
from smu_utils import SP_INDICATOR

from smu_info_loader import SMUInfoLoader
from bsd_service import BSDServiceHandler
from itsdangerous import TimedJSONWebSignatureSerializer as Serializer

import os
import io
import logging
import json
import datetime
import filters
import collections
import shutil
import re
import csv

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app)

#hook up the filters
filters.init(app)

app.secret_key = 'CSMSERVER'
    
# Use Flask-Login to track the current user in Flask's session.
login_manager = LoginManager()
login_manager.setup_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in.'
 
@login_manager.user_loader
def load_user(user_id):
    """Hook for Flask-Login to load a User instance from a user ID."""
    db_session = DBSession()
    return db_session.query(User).get(user_id)
    
@app.route('/')
@login_required
def home():
    db_session = DBSession()

    hosts = get_host_list(db_session)
    if hosts is None:
        abort(404)
    
    jump_hosts = get_jump_host_list(db_session)
    regions = get_region_list(db_session)
    servers = get_server_list(db_session)
    system_option = SystemOption.get(db_session)
    
    form = DialogServerForm(request.form)
    fill_servers(form.dialog_server.choices, get_server_list(DBSession()))

    return render_template('host/home.html', form=form, hosts=hosts, jump_hosts=jump_hosts, regions=regions, 
        servers=servers, hosts_info_json=get_host_platform_json(hosts), system_option=system_option, current_user=current_user) 

def get_host_platform_json(hosts):
    host_dict = {}
    for host in hosts:
        key = '{}={}'.format(host.software_version, host.software_platform)
        if key in host_dict:
            host_dict[key] += 1
        else:
            host_dict[key] = 1
    
    sorted_dict = collections.OrderedDict(sorted(host_dict.items()))
    
    rows = []
    # key is a tuple ('4.2.3-asr9k', 1)
    for key in sorted_dict.items():
        row = {}
        info_array = key[0].split('=')
        row['software'] = info_array[0]
        row['platform'] = info_array[1]
        row['count'] = key[1]
        rows.append(row)
    
    return json.dumps(rows)
    
@app.route('/install_dashboard')
@login_required
def install_dashboard():
    db_session = DBSession()

    hosts = get_host_list(db_session)
    if hosts is None:
        abort(404)
            
    return render_template('host/install_dashboard.html', hosts=hosts) 

@app.route('/users/create' , methods=['GET','POST'])
@login_required
def user_create():
    if not can_create(current_user):
        abort(401)
        
    form = UserForm(request.form)
    # Need to add the Required flag back as it is globally removed during user_edit()
    add_validator(form.password, Required) 
    
    if request.method == 'POST' and form.validate():
        db_session = DBSession()
        user = get_user(db_session, form.username.data)

        if user is not None:
            return render_template('user/edit.html', form=form, duplicate_error=True)
        
        user = User(
            username=form.username.data,
            password=form.password.data,
            privilege=form.privilege.data,
            fullname=form.fullname.data,
            email=form.email.data)
        
        user.preferences.append(Preferences())
        db_session.add(user)
        db_session.commit()
            
        return redirect(url_for('home'))
    else:
        return render_template('user/edit.html', form=form)

@app.route('/users/edit' , methods=['GET','POST'])
@login_required
def current_user_edit():
    return user_edit(current_user.username)

@app.route('/users/<username>/edit' , methods=['GET','POST'])
@login_required
def user_edit(username):
    db_session = DBSession()
    
    user = get_user(db_session, username)
    if user is None:
        abort(404)
        
    form = UserForm(request.form)
    # If not admin user, remove the privilege UI
    # because only admin user can change privilege.
    if current_user.privilege != UserPrivilege.ADMIN:  
        del form.privilege
            
    if request.method == 'POST' and form.validate():

        if len(form.password.data) > 0:
            user.password = form.password.data
        
        # Only admin user can change privilege
        if current_user.privilege == UserPrivilege.ADMIN and \
            current_user.username != user.username:
            user.privilege = form.privilege.data
            
        user.fullname = form.fullname.data
        user.email = form.email.data
            
        db_session.commit()
        return redirect(url_for('home'))
    else:
        form.username.data = user.username
        # Remove the Required flag so validation won't fail.  In edit mode, it is okay
        # not to provide the password.  In this case, the password on file is used.      
        remove_validator(form.password, Required) 
        
        if form.privilege is not None:
            form.privilege.data = user.privilege 
            
        form.fullname.data = user.fullname
        form.email.data = user.email

    return render_template('user/edit.html', form=form)

@app.route('/users/')
@login_required
def user_list():
    db_session = DBSession()

    users = get_user_list(db_session)
    if users is None:
        abort(404)
     
    if current_user.privilege == UserPrivilege.ADMIN:    
        return render_template('user/index.html', users=users)
    
    return render_template('user/not_authorized.html', user=current_user)

@app.route('/users/<username>/delete/', methods=['DELETE'])
@login_required
def user_delete(username):
    db_session = DBSession()

    user = get_user(db_session, username)
    if user is None:
        abort(404)
     
    db_session.delete(user)
    db_session.commit()
        
    return jsonify({'status':'OK'})

@app.route('/login/', methods=['GET', 'POST'])
def login():
    
    form = LoginForm(request.form)
    error_message = None
    
    if request.method == 'POST' and form.validate():
        username = form.username.data.strip()
        password = form.password.data.strip()
        
        db_session = DBSession()
     
        user, authenticated = \
            User.authenticate(db_session.query, username, password)
            
        if authenticated:
            login_user(user)
            
            # Certain admin features (Admin Console/Create or Edit User require 
            # re-authentication. The return_url indicates which admin feature the 
            # user wants to access.
            return_url = get_return_url(request)
            if return_url is None:              
                return redirect(request.args.get("next") or url_for('home'))
            else:
                return redirect(url_for(return_url))
        else:
            error_message = 'Incorrect username or password. Try again.'       
 
    # Fill the username if the user is still logged in.
    username = get_username(current_user)
    if username is not None:
        form.username.data = username
        
    return render_template('user/login.html', form=form, error_message=error_message, username=username)

"""
Return the current username.  If the user already logged out, return None
"""
def get_username(current_user):
    try:
        return current_user.username
    except:
        return None
    
@app.route('/logout/')
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/hosts/')
@login_required
def host_list():
    db_session = DBSession()

    hosts = get_host_list(db_session)
    if hosts is None:
        abort(404)
            
    return render_template('host/index.html', hosts=hosts)

@app.route('/hosts/import/', methods=['GET','POST'])
@login_required
def import_hosts(): 
    if not can_create(current_user):
        abort(401)
        
    form = HostImportForm(request.form)
    fill_regions(form.region.choices)
    
    if request.method == 'POST' and form.validate():
        return redirect(url_for('home'))
    
    return render_template('host/import.html', form=form) 

@app.route('/api/import_hosts', methods=['POST'])
@login_required
def api_import_hosts(): 
    db_session = DBSession()
   
    expected_header = 'hostname,ip,username,password,connection,port'.split(',')
    platform = data_list = request.form['platform']
    region_id = data_list = request.form['region']
    data_list = request.form['data_list']
    
    region = get_region_by_id(db_session, region_id)
    if region is None:
        return jsonify({'status':'Region is no longer exists in the database.'}) 
    
    header = None
    error = None
    im_hosts = []
    row_number = 0
    
    reader = csv.reader(data_list.split('\n'), delimiter=',')  
    for row in reader:
        row_number += 1
        
        if row_number == 1:
            # Check if header is correct
            header = row
            if 'hostname' not in header:
                error = '"hostname" is missing in the header.'
                break
            
            if 'ip' not in header:
                error = '"ip" is missing in the header.'
                break
            
            if 'connection' not in header:
                error = '"connection" is missing in the header.'
                break
                
            for header_field in header:                
                if header_field not in expected_header:
                    error = header_field + ' is not a correct header field.'
                    break
        else:
            if len(row) > 0:
                if (len(row) != len(header)):
                    error = '"' + ','.join(row) + '" has wrong number of data fields.'
                    break
                
                im_host = Host()
                im_host.platform = platform
                im_host.region_id = region_id
                im_host.created_by = current_user.username
                im_host.inventory_job.append(InventoryJob())
                im_host.connection_param.append(ConnectionParam())
                
                im_host.connection_param[0].username = ''
                im_host.connection_param[0].password = ''
                
                for column in range(len(header)):
                    
                    header_field = header[column]
                    data_field = row[column]
                    
                    if header_field == 'hostname':
                        # Check if the hostname exists already
                        if get_host(db_session, data_field) is not None:
                            error = 'hostname "' + data_field + '" already exists in the database.'
                            break
                        
                        if data_field in im_hosts:
                            error = 'hostname "' + data_field + '" already exists in the import data.'
                            break
                        
                        im_hosts.append(data_field)
                        im_host.hostname = data_field
                    elif header_field == 'ip':
                        im_host.connection_param[0].host_or_ip = data_field
                    elif header_field == 'username':
                        im_host.connection_param[0].username = data_field
                    elif header_field == 'password':
                        im_host.connection_param[0].password = data_field
                    elif header_field == 'connection':
                        if data_field != ConnectionType.TELNET and data_field != ConnectionType.SSH:
                            error = '"' + ','.join(row) + '" has a wrong connection type (should be "telnet" or "ssh").'
                            break
                        im_host.connection_param[0].connection_type = data_field
                    elif header_field == 'port':
                        im_host.connection_param[0].port_number = data_field
                        
        
                # Add the import host
                db_session.add(im_host)
     
    if error is not None:
        return jsonify({'status':error})
    else:
        db_session.commit()
        return jsonify({'status':'OK'})

@app.route('/hosts/create/', methods=['GET', 'POST'])
@login_required
def host_create():    
    if not can_create(current_user):
        abort(401)
        
    form = HostForm(request.form) 
    
    fill_jump_hosts(form.jump_host.choices)
    fill_regions(form.region.choices)
    
    if request.method == 'POST' and form.validate():
        db_session = DBSession()
        try:
            host = get_host(db_session, form.hostname.data)
            if host is not None:
                return render_template('host/edit.html', form=form, duplicate_error=True)
            
            host = Host(hostname=form.hostname.data, 
            platform=form.platform.data,
            created_by=current_user.username)
        
            host.inventory_job.append(InventoryJob())
            db_session.add(host)
            
            host.region_id = form.region.data if form.region.data > 0 else None
            host.roles = remove_extra_spaces(form.roles.data)
            host.connection_param = [ConnectionParam(
                # could have multiple IPs, separated by comma
                host_or_ip = remove_extra_spaces(form.host_or_ip.data),
                username = form.username.data,
                password = form.password.data,
                jump_host_id = form.jump_host.data if form.jump_host.data > 0 else None,
                connection_type = form.connection_type.data,
                # could have multiple ports, separated by comma
                port_number = remove_extra_spaces(form.port_number.data))]
                            
            db_session.commit()

        finally:
            db_session.rollback()
            
        return redirect(url_for('home') ) 

    return render_template('host/edit.html', form=form)

"""
Given a comma delimited string and remove extra spaces
Example: 'x   x  ,   y,  z' becomes 'x x,y,z'
"""
def remove_extra_spaces(str):
    if str is not None:
        return ','.join([re.sub(r'\s+', ' ', x).strip() for x in str.split(',')])
    return str
    
@app.route('/hosts/<hostname>/edit/', methods=['GET', 'POST'])
@login_required
def host_edit(hostname):        
    db_session = DBSession()
    
    host = get_host(db_session, hostname)
    if host is None:
        abort(404)
        
    form = HostForm(request.form)
    fill_jump_hosts(form.jump_host.choices)
    fill_regions(form.region.choices)

    if request.method == 'POST' and form.validate():
        if not can_edit(current_user):
            abort(401)
        
        if hostname != form.hostname.data and \
            get_host(db_session, form.hostname.data) is not None:
            return render_template('host/edit.html', form=form, duplicate_error=True)
        
        host.hostname = form.hostname.data
        host.platform = form.platform.data
        host.region_id = form.region.data if form.region.data > 0 else None
        host.roles = remove_extra_spaces(form.roles.data) 
        
        connection_param = host.connection_param[0]
        # could have multiple IPs, separated by comma
        connection_param.host_or_ip = remove_extra_spaces(form.host_or_ip.data) 
        connection_param.username = form.username.data
        if len(form.password.data) > 0:
            connection_param.password = form.password.data
        connection_param.jump_host_id = form.jump_host.data if form.jump_host.data > 0 else None
        connection_param.connection_type = form.connection_type.data
        # could have multiple ports, separated by comma
        connection_param.port_number = remove_extra_spaces(form.port_number.data)
        db_session.commit()
        
        return_url = get_return_url(request, 'home')
        if return_url is None:
            return redirect(url_for('home') )
        else:
            return redirect(url_for(return_url, hostname=hostname))
    else:
        # Assign the values to form fields
        form.hostname.data = host.hostname
        form.platform.data = host.platform
        form.region.data = host.region_id
        form.roles.data = host.roles
        form.host_or_ip.data = host.connection_param[0].host_or_ip
        form.username.data = host.connection_param[0].username
        form.jump_host.data = host.connection_param[0].jump_host_id
        form.connection_type.data = host.connection_param[0].connection_type
        form.port_number.data = host.connection_param[0].port_number
        
        return render_template('host/edit.html', form=form)
   
@app.route('/hosts/<hostname>/delete/', methods=['DELETE'])
@login_required
def host_delete(hostname):
    if not can_delete(current_user):
        abort(401)
        
    db_session = DBSession()

    host = get_host(db_session, hostname)
    if host is None:
        abort(404)
    
    delete_host_inventory_job_session_logs(db_session, host) 
    delete_host_install_job_session_logs(db_session, host)
    db_session.delete(host)
    db_session.commit()
        
    return jsonify({'status':'OK'})

def delete_host_inventory_job_session_logs(db_session, host):
    inventory_jobs = db_session.query(InventoryJobHistory).filter(InventoryJobHistory.host_id == host.id)
    for inventory_job in inventory_jobs:
        try:
            shutil.rmtree(get_autlogs_directory() + inventory_job.session_log)   
        except:
            logger.exception('delete_host_inventory_job_session_logs hit exception')
            
def delete_host_install_job_session_logs(db_session, host):
    install_jobs = db_session.query(InstallJobHistory).filter(InstallJobHistory.host_id == host.id)
    for install_job in install_jobs:
        try:
            shutil.rmtree(get_autlogs_directory() + install_job.session_log)   
        except:
            logger.exception('delete_host_install_job_session_logs hit exception')

@app.route('/jump_hosts/')
@login_required
def jump_host_list():
    db_session = DBSession()
  
    hosts = get_jump_host_list(db_session)
    if hosts is None:
        abort(404)
            
    return render_template('jump_host/index.html', hosts=hosts)

@app.route('/jump_hosts/create/', methods=['GET', 'POST'])
@login_required
def jump_host_create():
    if not can_create(current_user):
        abort(401)
        
    form = JumpHostForm(request.form)
    
    if request.method == 'POST' and form.validate():           
        db_session = DBSession()       
        host = get_jump_host(db_session, form.hostname.data)
        if host is not None:
            return render_template('jump_host/edit.html', form=form, duplicate_error=True)
        
        host = JumpHost(
            hostname = form.hostname.data,
            host_or_ip = form.host_or_ip.data,
            username = form.username.data,
            password = form.password.data,
            connection_type = form.connection_type.data,
            port_number = form.port_number.data,
            created_by = current_user.username)
          
        db_session.add(host)
        db_session.commit() 
            
        return redirect(url_for('home') )
                
    return render_template('jump_host/edit.html', form=form)

@app.route('/jump_hosts/<hostname>/edit/', methods=['GET', 'POST'])
@login_required
def jump_host_edit(hostname):        
    db_session = DBSession()
    
    host = get_jump_host(db_session, hostname)
    if host is None:
        abort(404)
        
    form = JumpHostForm(request.form, host)

    if request.method == 'POST' and form.validate():
        if not can_edit(current_user):
            abort(401)
        
        if hostname != form.hostname.data and \
            get_jump_host(db_session, form.hostname.data) is not None:
            return render_template('jump_host/edit.html', form=form, duplicate_error=True)
        
        host.hostname = form.hostname.data
        host.host_or_ip = form.host_or_ip.data
        host.username = form.username.data
        if len(form.password.data) > 0:
            host.password = form.password.data
        host.connection_type = form.connection_type.data
        host.port_number = form.port_number.data
        db_session.commit()
        
        return redirect(url_for('home') )
    else:
        # Assign the values to form fields
        form.hostname.data = host.hostname
        form.host_or_ip.data = host.host_or_ip
        form.username.data = host.username
        form.connection_type.data = host.connection_type
        form.port_number.data = host.port_number
        
        return render_template('jump_host/edit.html', form=form)

@app.route('/jump_hosts/<hostname>/delete/', methods=['DELETE'])
@login_required
def jump_host_delete(hostname):
    if not can_delete(current_user):
        abort(401)
        
    db_session = DBSession()
    
    host = get_jump_host(db_session, hostname)
    if host is None:
        abort(404)
        
    db_session.delete(host)
    db_session.commit()
        
    return jsonify({'status':'OK'})

@app.route('/servers/create/', methods=['GET', 'POST'])
@login_required
def server_create():
    if not can_create(current_user):
        abort(401)
        
    form = ServerForm(request.form)
    
    if request.method == 'POST' and form.validate():          
        db_session = DBSession()        
        server = get_server(db_session, form.hostname.data)
        if server is not None:
            return render_template('server/edit.html', form=form, duplicate_error=True) 
        
        server = Server(
            hostname = form.hostname.data,
            server_type=form.server_type.data,
            server_url=trim_last_slash(form.server_url.data), 
            username=form.username.data,
            password=form.password.data,
            server_directory=trim_last_slash(form.server_directory.data),
            created_by=current_user.username)
            
        db_session.add(server)
        db_session.commit()
            
        return redirect(url_for('home') )

    return render_template('server/edit.html', form=form)

@app.route('/servers/<hostname>/edit/', methods=['GET', 'POST'])
@login_required
def server_edit(hostname):        
    db_session = DBSession()
    
    server = get_server(db_session, hostname)
    if server is None:
        abort(404)
        
    form = ServerForm(request.form)

    if request.method == 'POST' and form.validate():
        if not can_edit(current_user):
            abort(401)
        
        if hostname != form.hostname.data and \
            get_server(db_session, form.hostname.data) is not None:
            return render_template('server/edit.html', form=form, duplicate_error=True)
        
        server.hostname = form.hostname.data
        server.server_type = form.server_type.data
        server.server_url = trim_last_slash(form.server_url.data)
        server.username = form.username.data
        if len(form.password.data) > 0:
            server.password = form.password.data
        server.server_directory = trim_last_slash(form.server_directory.data)
        db_session.commit()
        
        return redirect(url_for('home') )
    else:
        # Assign the values to form fields
        form.hostname.data = server.hostname
        form.server_type.data = server.server_type
        form.server_url.data = server.server_url
        form.username.data = server.username
        # In Edit mode, make the password field not required
        form.server_directory.data = server.server_directory
        
        return render_template('server/edit.html', form=form)
    

@app.route('/servers/<hostname>/delete/', methods=['DELETE'])
@login_required
def server_delete(hostname):
    if not can_delete(current_user):
        abort(401)
        
    db_session = DBSession()

    server = get_server(db_session, hostname)
    if server is None:
        abort(404)
     
    db_session.delete(server)
    db_session.commit()
        
    return jsonify({'status':'OK'})

@app.route('/regions/create/', methods=['GET', 'POST'])
@login_required
def region_create():
    if not can_create(current_user):
        abort(401)
        
    form = RegionForm(request.form)
    
    if request.method == 'POST' and form.validate():
            
        db_session = DBSession()        
        region = get_region(db_session, form.region_name.data)
                
        if region is not None:
            return render_template('region/edit.html', form=form, duplicate_error=True)   
            
        region = Region(
            name=form.region_name.data,
            created_by=current_user.username)
        
        server_id_list = request.form.getlist('selected-servers')
        for server_id in server_id_list:
            server = get_server_by_id(db_session, server_id)
            if server is not None:
                region.servers.append(server)

        db_session.add(region)
        db_session.commit()   
                   
        return redirect(url_for('home') ) 
    
    return render_template('region/edit.html', form=form)

@app.route('/regions/<region_name>/edit/', methods=['GET', 'POST'])
@login_required
def region_edit(region_name):       
    db_session = DBSession()
    
    region = get_region(db_session, region_name)
    if region is None:
        abort(404)
        
    form = RegionForm(request.form)

    if request.method == 'POST' and form.validate():
        if not can_edit(current_user):
            abort(401)
        
        if region_name != form.region_name.data and \
            get_region(db_session, form.region_name.data) is not None:
            return render_template('region/edit.html', form=form, duplicate_error=True)
        
        region.name = form.region_name.data
        region.servers = []
        server_id_list = request.form.getlist('selected-servers')
        for server_id in server_id_list:
            server = get_server_by_id(db_session, server_id)
            if server is not None:
                region.servers.append(server)
        
        db_session.commit()
        
        return redirect(url_for('home') )
    else:
        form.region_name.data = region.name

        return render_template('region/edit.html', form=form, region=region)

@app.route('/regions/<region_name>/delete/', methods=['DELETE'])
@login_required
def region_delete(region_name):
    if not can_delete(current_user):
        abort(401)
        
    db_session = DBSession()

    region = get_region(db_session, region_name)
    if region is None:
        abort(404)
     
    db_session.delete(region)
    db_session.commit()
        
    return jsonify({'status':'OK'})

def add_validator(field, validator_class):
    validators = field.validators
    for v in validators:
        if isinstance(v, validator_class):
            return
        
    validators.append(validator_class())
            
def remove_validator(field, validator_class):
    validators = field.validators
    for v in validators:
        if isinstance(v, validator_class):
            validators.remove(v) 


@app.route('/api/hosts/<hostname>/packages/<package_state>', methods=['GET','POST'])
@login_required
def api_get_host_dashboard_packages(hostname, package_state):
    db_session = DBSession()
    host = get_host(db_session, hostname)
    
    rows = []       
    if host is not None:
        packages = db_session.query(Package).filter(and_(Package.host_id == host.id, Package.state == package_state)). \
            order_by(Package.name).all()
            
        has_module_packages = False
        for package in packages:        
            if len(package.modules_package_state) > 0:
                has_module_packages = True
                break
            
        if has_module_packages:
            module_package_dict = {}
            
            # Format it from module, then packages
            for package in packages:
                package_name = package.name if package.location is None else  package.location + ':' + package.name
                for modules_package_state in package.modules_package_state:
                    module = modules_package_state.module_name
                    if module in module_package_dict:
                        module_package_dict[module].append(package_name)
                    else:
                        package_list = []
                        package_list.append(package_name)
                        module_package_dict[module] = package_list
            
            sorted_dict = collections.OrderedDict(sorted(module_package_dict.items()))
            
            for module in sorted_dict:
                package_list = sorted_dict[module]
                rows.append({ 'package' : module })
                for package_name in package_list:  
                    rows.append({ 'package' : ('&nbsp;' * 7) + package_name })
            
        else:
            for package in packages:
                rows.append( {'package' : package.name if package.location is None else  package.location + ':' + package.name} ) 

    return jsonify( **{'data':rows} )

"""
Returns scheduled installs for a host in JSON format.
"""
@app.route('/api/hosts/<hostname>/scheduled_installs', methods=['GET','POST'])
@login_required
def api_get_host_dashboard_scheduled_install(hostname):
    db_session = DBSession()
    host = get_host(db_session, hostname)
    
    rows = []       
    if host is not None and len(host.install_job) > 0:
        for install_job in host.install_job:
            row = {}
            row['hostname'] = host.hostname
            row['install_job_id'] = install_job.id
            row['install_action'] = install_job.install_action
            row['scheduled_time'] = install_job.scheduled_time
            row['session_log'] = install_job.session_log
            row['status'] = install_job.status

            rows.append(row)
            
    return jsonify( **{'data':rows} )

@app.route('/api/hosts/<hostname>/host_dashboard/cookie', methods=['GET', 'POST'])
@login_required
def api_get_host_dashboard_cookie(hostname):
    db_session = DBSession()
    host = get_host(db_session, hostname)

    rows = []
    if host is not None:
        system_option = SystemOption.get(db_session)
        row = {}
        connection_param = host.connection_param[0]
        row['hostname'] = host.hostname
        row['region'] = host.region.name
        row['roles'] = host.roles
        row['platform'] = host.platform
        row['software'] = host.software_version
        row['host_or_ip'] = connection_param.host_or_ip
        row['username'] = connection_param.username
        row['connection'] = connection_param.connection_type
        row['port_number'] = connection_param.port_number
        row['created_by'] = host.created_by
        
        if connection_param.jump_host is not None:
            row['jump_host'] = connection_param.jump_host.hostname
            
        # Last inventory successful time
        inventory_job = host.inventory_job[0]
        if inventory_job.pending_submit:
            row['since_last_successful_inventory_time'] = 'Pending Retrieval'
        else:
            row['since_last_successful_inventory_time'] = time_difference_UTC(inventory_job.last_successful_time)
            
        row['last_successful_inventory_time'] = inventory_job.last_successful_time
        
        install_job_history_count = 0
        if host is not None:
            install_job_history_count = db_session.query(InstallJobHistory).count()
        
        row['last_install_job_history_count'] = install_job_history_count
        row['can_schedule'] = system_option.can_schedule
        rows.append(row)
    
    return jsonify( **{'data':rows} )
    
@app.route('/api/hosts/<hostname>/install_job_history', methods=['GET'])
@login_required
def api_get_host_dashboard_install_job_history(hostname):
    rows = [] 
    db_session = DBSession()
    
    host = get_host(db_session, hostname)
    if host is not None:  
        record_limit = request.args.get('record_limit')
        if record_limit is None or record_limit.lower() == 'all':  
            install_jobs = db_session.query(InstallJobHistory).filter(InstallJobHistory.host_id == host.id). \
                order_by(InstallJobHistory.created_time.desc())
        else:  
            install_jobs = db_session.query(InstallJobHistory).filter(InstallJobHistory.host_id == host.id). \
                order_by(InstallJobHistory.created_time.desc()).limit(record_limit)

        return jsonify(**get_install_job_json_dict(install_jobs))
    
    return jsonify( **{'data':rows} )

@app.route('/api/hosts/<hostname>/software_inventory_history', methods=['GET'])
@login_required
def api_get_host_dashboard_software_inventory_history(hostname):
    rows = [] 
    db_session = DBSession()
    
    host = get_host(db_session, hostname)
    if host is not None:  
        record_limit = request.args.get('record_limit')
        if record_limit is None or record_limit.lower() == 'all':  
            inventory_jobs = db_session.query(InventoryJobHistory).filter(InventoryJobHistory.host_id == host.id). \
                order_by(InventoryJobHistory.created_time.desc())
        else:  
            inventory_jobs = db_session.query(InventoryJobHistory).filter(InventoryJobHistory.host_id == host.id). \
                order_by(InventoryJobHistory.created_time.desc()).limit(record_limit)

        return jsonify(**get_inventory_job_json_dict(inventory_jobs))
    
    return jsonify( **{'data':rows} )

def get_inventory_job_json_dict(inventory_jobs):
    rows = []    
    for inventory_job in inventory_jobs:
        row = {}
        row['hostname'] = inventory_job.host.hostname
        row['status'] = inventory_job.status
        row['status_time'] = inventory_job.status_time  
        row['elapsed_time'] = time_difference_UTC(inventory_job.status_time)
        row['inventory_job_id'] = inventory_job.id
            
        if inventory_job.session_log is not None:
            row['session_log'] = inventory_job.session_log
             
        if inventory_job.trace is not None:
            row['trace'] = inventory_job.id
                  
        rows.append(row)
       
    return {'data':rows}
    
@app.route('/api/install_scheduled/')
@login_required
def api_get_install_scheduled():
    db_session = DBSession()
    
    install_jobs = db_session.query(InstallJob).filter( \
        (InstallJob.install_action is not None), \
        and_(InstallJob.status == None))

    return jsonify(**get_install_job_json_dict(install_jobs))

@app.route('/api/install_in_progress/')
@login_required
def api_get_install_in_progress():
    db_session = DBSession()    
    install_jobs = db_session.query(InstallJob).filter(and_( \
    InstallJob.status != None, \
    InstallJob.status != JobStatus.FAILED) )

    return jsonify(**get_install_job_json_dict(install_jobs))

@app.route('/api/install_failed/')
@login_required
def api_get_install_failed():
    db_session = DBSession()   
    install_jobs = db_session.query(InstallJob).filter(InstallJob.status == JobStatus.FAILED)

    return jsonify(**get_install_job_json_dict(install_jobs))

def get_download_job_status(download_job_key_dict, install_job, dependency_status):
    num_pending_downloads = 0
    num_failed_downloads = 0
    is_pending_download = False
    
    pending_downloads = install_job.pending_downloads.split(',')
    for filename in pending_downloads:
        download_job_key = get_download_job_key(install_job.user_id, filename, install_job.server_id, install_job.server_directory)
        if download_job_key in download_job_key_dict:
            download_job = download_job_key_dict[download_job_key]
            if download_job.status == JobStatus.FAILED:
                num_failed_downloads += 1
            else:
                num_pending_downloads += 1
            is_pending_download = True

    if is_pending_download:
        job_status = "({} pending".format(num_pending_downloads)
        if num_failed_downloads > 0:
            job_status = "{},{} failed)".format(job_status, num_failed_downloads)
        else:
            job_status = "{})".format(job_status)
            
        #job_status = '(Failed)' if is_download_failed else ''
        download_url = '<a href="' + url_for('download_dashboard') + '">Pending Download ' + job_status + '</a>'
        if len(dependency_status) > 0:
            dependency_status = '{}<br/>{}'.format(dependency_status, download_url)
        else:
            dependency_status = download_url
            
    return dependency_status
                        
"""
install_jobs is a list of install_job instances from the install_job or install_job_history table.
Schema in the install_job_history table is a superset of the install_job table. 
"""
def get_install_job_json_dict(install_jobs):
    download_job_key_dict = get_download_job_key_dict()
    rows = []    
    for install_job in install_jobs:
        if isinstance(install_job, InstallJob) or isinstance(install_job, InstallJobHistory):
            row = {}
            row['install_job_id'] = install_job.id
            row['hostname'] = install_job.host.hostname
            row['dependency'] = '' if install_job.dependency is None else 'Another Installation'
            row['install_action'] = install_job.install_action
            row['scheduled_time'] = install_job.scheduled_time

            # Retrieve the pending download status of the scheduled download job.
            # The install job has not been started if its status is None.
            if install_job.status is None:
                row['dependency'] = get_download_job_status(download_job_key_dict, install_job, row['dependency'])

            row['start_time'] = install_job.start_time
            row['packages'] = install_job.packages
                           
            if isinstance(install_job, InstallJob):
                row['server_id'] = install_job.server_id
                row['server_directory'] = install_job.server_directory
                row['user_id'] = install_job.user_id
            
            row['status'] = install_job.status
            row['status_time'] = install_job.status_time               
            row['created_by'] = install_job.created_by
            
            if install_job.session_log is not None:
                row['session_log'] = install_job.session_log
                
            if install_job.trace is not None:
                row['trace'] = install_job.id
                  
            rows.append(row)
       
    return {'data':rows}

@app.route('/api/get_files_from_csm_repository/')
@login_required
def get_files_from_csm_repository():
    result_list = {}
    file_list = get_file_list(get_repository_directory())
    
    for filename in file_list:
        if '.size' in filename:
            recorded_size = open(get_repository_directory() + filename, 'r').read()
            # Pick out the tar files that have been download completely
            result_list[filename.replace('.size','')] = recorded_size
    
    rows = []  
    for filename in file_list:
        if filename in result_list:
            row = {}
            row['image_name'] = filename
            row['image_size'] = result_list[filename] + ' bytes'
            rows.append(row)
    
    return jsonify( **{'data':rows} )

@app.route('/api/image/<image_name>/delete/' , methods=['DELETE'])
@login_required  
def api_delete_image_from_repository(image_name):
    if current_user.privilege != UserPrivilege.ADMIN:
        abort(401)
        
    tar_image_path = get_repository_directory() + image_name
    file_list = get_tarfile_file_list(tar_image_path)
    for filename in file_list:
        try:
            os.remove(get_repository_directory() + filename) 
        except:
            pass
       
    try:
        os.remove(tar_image_path) 
        os.remove(tar_image_path + '.size')
    except:
        logger.exception('api_delete_image_from_repository hit exception')
        return jsonify({'status':'Failed'})
    
    return jsonify({'status':'OK'})
    
@app.route('/api/download_scheduled/')
@login_required
def api_get_download_scheduled():  
    db_session = DBSession()
    download_jobs = db_session.query(DownloadJob).filter(DownloadJob.status == None)

    return jsonify(**get_download_job_json_dict(db_session, download_jobs))

@app.route('/api/download_in_progress/')
@login_required
def api_get_download_in_progress():
    db_session = DBSession()    
    download_jobs = db_session.query(DownloadJob).filter(and_( \
    DownloadJob.status != None, \
    DownloadJob.status != JobStatus.FAILED) )

    return jsonify(**get_download_job_json_dict(db_session, download_jobs))

@app.route('/api/download_failed/')
@login_required
def api_get_download_failed():
    db_session = DBSession()   
    download_jobs = db_session.query(DownloadJob).filter(DownloadJob.status == JobStatus.FAILED)

    return jsonify(**get_download_job_json_dict(db_session, download_jobs))

@app.route('/api/download_completed/')
@login_required
def api_get_download_completed():
    db_session = DBSession()

    record_limit = request.args.get('record_limit')

    if record_limit is None or record_limit.lower() == 'all':   
        download_jobs = db_session.query(DownloadJobHistory).filter(DownloadJobHistory.status == JobStatus.COMPLETED). \
            order_by(DownloadJobHistory.status_time.desc())
    else:  
        download_jobs = db_session.query(DownloadJobHistory).filter(DownloadJobHistory.status == JobStatus.COMPLETED). \
            order_by(DownloadJobHistory.status_time.desc()).limit(record_limit)

    return jsonify(**get_download_job_json_dict(db_session, download_jobs))

@app.route('/api/download_dashboard/cookie')
@login_required
def api_get_download_dashboard_cookie():
    db_session = DBSession()

    completed_download_job_count = db_session.query(DownloadJobHistory).filter(
        DownloadJobHistory.status == JobStatus.COMPLETED).count()
    
    return jsonify( **{'data': [ {'last_completed_download_job_count': completed_download_job_count }] } )
    
def get_download_job_json_dict(db_session, download_jobs):
    rows = []    
    for download_job in download_jobs:
        if isinstance(download_job, DownloadJob) or isinstance(download_job, DownloadJobHistory):
            row = {}
            row['download_job_id'] = download_job.id
            row['image_name'] = download_job.cco_filename 
            row['scheduled_time'] = download_job.scheduled_time
            
            server = get_server_by_id(db_session, download_job.server_id)
            if server is not None:
                row['server_repository'] = server.hostname
                if not is_empty(download_job.server_directory):
                    row['server_repository'] = row['server_repository'] + '<br><span style="color: Gray;"><b>Sub-directory:</b></span> ' + download_job.server_directory
            else:
                row['server_repository'] = 'Unknown'
                
            row['status'] = download_job.status
            row['status_time'] = download_job.status_time               
            row['created_by'] = download_job.created_by
            
            if download_job.trace is not None:
                row['trace'] = download_job.id
                  
            rows.append(row)
       
    return {'data':rows}

@app.route('/api/resubmit_download_jobs/')
@login_required
def api_resubmit_download_jobs():
    if not can_install(current_user):
        abort(401)
     
    user_id = request.args.get("user_id")   
    server_id = request.args.get("server_id")
    server_directory = request.args.get("server_directory")
    
    db_session = DBSession()   
    download_jobs = db_session.query(DownloadJob).filter(and_(DownloadJob.user_id == user_id, 
        DownloadJob.server_id == server_id, DownloadJob.server_directory == server_directory))
    
    for download_job in download_jobs:
        download_job.status = None
        download_job.status_time = None        
    db_session.commit()
    
    return jsonify({'status':'OK'})

@app.route('/resubmit_download_job/<int:id>/', methods=['POST'])
@login_required
def resubmit_download_job(id):
    if not can_install(current_user):
        abort(401)
    
    db_session = DBSession()
    
    download_job = db_session.query(DownloadJob).filter(DownloadJob.id == id).first()
    if download_job is None:
        abort(404)   

    try: 
        # Download jobs that are in progress cannot be deleted.
        download_job.status = None
        download_job.status_time = None        
        db_session.commit()
        
        return jsonify({'status':'OK'})

    except:  
        logger.exception('resubmit_download_job hits exception')
        return jsonify({'status':'Failed: check system logs for details'})
        
@app.route('/delete_download_job/<int:id>/', methods=['DELETE'])
@login_required
def delete_download_job(id):
    if not can_delete_install(current_user):
        abort(401)
        
    db_session = DBSession()
    
    download_job = db_session.query(DownloadJob).filter(DownloadJob.id == id).first()
    if download_job is None:
        abort(404)
        
    try: 
        # Download jobs that are in progress cannot be deleted.
        if download_job.status is None or download_job.status == JobStatus.FAILED:
            db_session.delete(download_job)            
            db_session.commit()
        
        return jsonify({'status':'OK'})

    except:  
        logger.exception('delete download job hits exception')
        return jsonify({'status':'Failed: check system logs for details'})

@app.route('/hosts/delete_all_failed_downloads/', methods=['DELETE'])
@login_required
def delete_all_failed_downloads():
    if not can_delete_install(current_user):
        abort(401)
        
    return delete_all_downloads(status=JobStatus.FAILED)

@app.route('/hosts/delete_all_scheduled_downloads/', methods=['DELETE'])
@login_required
def delete_all_scheduled_downloads():
    if not can_delete_install(current_user):
        abort(401)
        
    return delete_all_downloads()
    
def delete_all_downloads(status=None):        
    db_session = DBSession()
    
    try:
        download_jobs = db_session.query(DownloadJob).filter(DownloadJob.status == status)    
        for download_job in download_jobs:
            db_session.delete(download_job)
        
        db_session.commit()    

        return jsonify({'status':'OK'})
    except:
        logger.exception('delete download job hits exception')
        return jsonify({'status':'Failed: check system logs for details'})
    
@app.route('/api/get_server_time/')
@login_required
def api_get_server_time():  
    dict = {}
    dict['server_time'] = datetime.datetime.utcnow()

    return jsonify( **{'data':dict} )

"""
This method is called by ajax attached to Select2 (Search a host).
The returned JSON contains the predefined tags.
"""
@app.route('/api/get_hostnames/')
@login_required
def api_get_hostnames():  
    db_session = DBSession()
    
    rows = []   
    criteria='%'
    if len(request.args) > 0:
        criteria += request.args.get('q') + '%'
 
    hosts = db_session.query(Host).filter(Host.hostname.like(criteria)).order_by(Host.hostname.asc()).all()   
    if len(hosts) > 0:
        for host in hosts:
            row = {}
            row['id'] = host.hostname
            row['text'] = host.hostname
            rows.append(row)
  
    return jsonify( **{'data':rows} )

"""
Returns a list of unique Scheduled Time (in UTC) from the Job History.  
"""
def get_install_scheduled_time_list():
    result_list = []
    db_session = DBSession()
    
    scheduled_times = db_session.query(InstallJobHistory.scheduled_time).order_by(InstallJobHistory.scheduled_time.desc()).distinct()
    if scheduled_times is not None:
        for scheduled_time in scheduled_times:
            scheduled_time_string = get_datetime_string(scheduled_time[0])
            if scheduled_time_string not in result_list:
                result_list.append(scheduled_time_string)
            
    return result_list
         
@app.route('/api/install_completed/')
@login_required
def api_get_install_completed():
    db_session = DBSession()

    record_limit = request.args.get('record_limit')

    if record_limit is None or record_limit.lower() == 'all':  
        install_jobs = db_session.query(InstallJobHistory).filter(InstallJobHistory.status == JobStatus.COMPLETED). \
            order_by(InstallJobHistory.status_time.desc())
    else:  
        install_jobs = db_session.query(InstallJobHistory).filter(InstallJobHistory.status == JobStatus.COMPLETED). \
            order_by(InstallJobHistory.status_time.desc()).limit(record_limit)
              
    return jsonify(**get_install_job_json_dict(install_jobs))

@app.route('/api/install_dashboard/cookie')
@login_required
def api_get_install_dashboard_cookie():
    db_session = DBSession()
    system_option = SystemOption.get(db_session)

    completed_install_job_count = db_session.query(InstallJobHistory).filter(
        InstallJobHistory.status == JobStatus.COMPLETED).count()
    
    return jsonify( **{'data': [ 
        {'last_completed_install_job_count': completed_install_job_count,
         'can_schedule' : system_option.can_schedule,
         'can_install' : system_option.can_install
        }] } )
    
"""
For batch scheduled installation.
"""
@app.route('/hosts/schedule_install/', methods=['GET', 'POST'])
@login_required
def schedule_install():
    if not can_install(current_user):
        abort(401)
        
    db_session = DBSession()
    form = ScheduleInstallForm(request.form)
    
    # Fills the selections
    fill_install_actions(choices=form.install_action.choices, include_ALL=True)
    fill_regions(form.region.choices)
    fill_dependencies(form.dependency.choices)
    fill_servers(form.server.choices, get_server_list(db_session))
    fill_servers(form.dialog_server.choices, get_server_list(db_session))
    
    return_url = get_return_url(request, 'home')
    
    if request.method == 'POST': # and form.validate():
        # Retrieves from the multi-select box
        hostnames = request.form.getlist('selected-hosts')
        install_action = form.install_action.data 
        
        if hostnames is not None:
            for hostname in hostnames:
                host = get_host(db_session, hostname)  
                if host is not None:
                    db_session = DBSession()

                    if InstallAction.ALL in install_action:
                        create_install_jobs_for_all_install_actions(db_session=db_session, host_id=host.id, form=form)
                    else:        
                        # If only one install_action, accept the selected dependency if any
                        dependency = 0
                        if len(install_action) == 1:
                            # No dependency when it is 0 (a digit)
                            if not form.dependency.data.isdigit():
                                prerequisite_install_job = get_first_install_action(db_session, form.dependency.data)
                                if prerequisite_install_job is not None:
                                    dependency = prerequisite_install_job.id
                                    
                            create_or_update_install_job(db_session=db_session, host_id=host.id, form=form, 
                                install_action=install_action[0], dependency=dependency)
                        else:
                            # The dependency on each install action is already indicated in the implicit ordering in the selector.
                            # If the user selected Pre-Upgrade and Install Add, Install Add (successor) will 
                            # have Pre-Upgrade (predecessor) as the dependency.
                            dependency = 0              
                            for one_install_action in install_action:
                                new_install_job = create_or_update_install_job(db_session=db_session, host_id=host.id, form=form, 
                                    install_action=one_install_action, dependency=dependency)
                                dependency = new_install_job.id
                                                  
        return redirect(url_for(return_url))
    else:                
        # Initialize the hidden fields
        form.hidden_server_directory.data = '' 
        form.hidden_pending_downloads.data = ''
            
        return render_template('schedule_install.html', form=form, 
            server_time=datetime.datetime.utcnow(), return_url=return_url)

def get_first_install_action(db_session, install_action):
    return db_session.query(InstallJob).filter(InstallJob.install_action == install_action). \
        order_by(InstallJob.scheduled_time.asc()).first()
                                                       
@app.route('/hosts/<hostname>/schedule_install/', methods=['GET', 'POST'])
@login_required
def host_schedule_install(hostname):
    if not can_install(current_user):
        abort(401)
        
    return handle_schedule_install_form(request=request, db_session=DBSession(), hostname=hostname)

@app.route('/hosts/<hostname>/schedule_install/<int:id>/edit/', methods=['GET', 'POST'])
@login_required
def host_schedule_install_edit(hostname, id):
    if not can_edit_install(current_user):
        abort(401)
        
    db_session = DBSession()
   
    install_job = db_session.query(InstallJob).filter(InstallJob.id == id).first()
    if install_job is None:
        abort(404)
    
    return handle_schedule_install_form(request=request, db_session=db_session, hostname=hostname, install_job=install_job)

def create_or_update_install_job(db_session, host_id, form, install_action, dependency=0, install_job=None):
    # This is a new install_job
    if install_job is None:
        install_job = InstallJob()
        install_job.host_id = host_id
        db_session.add(install_job)
    
    install_job.install_action = install_action
        
    if install_job.install_action == InstallAction.INSTALL_ADD and \
        not is_empty(form.hidden_pending_downloads.data):
        install_job.pending_downloads = ','.join(form.hidden_pending_downloads.data.split())
    else:
        install_job.pending_downloads = ''

    install_job.scheduled_time = get_datetime(form.scheduled_time_UTC.data, "%m/%d/%Y %I:%M %p")  
    install_job.server_id = form.server.data if form.server.data > 0 else None
    install_job.server_directory = form.hidden_server_directory.data

    # Form a comma delimited list from newlines
    install_job.packages = ','.join(form.software_packages.data.split())
    install_job.dependency = dependency if dependency > 0 else None
    install_job.created_by = current_user.username
    install_job.user_id = current_user.id
         
    #Resets the following fields
    install_job.status = None
    install_job.status_time = None
    install_job.session_log = None
    install_job.trace = None
       
    if install_job.install_action != InstallAction.UNKNOWN:
        db_session.commit()
    
    # Creates download jobs if needed
    if install_job.install_action == InstallAction.INSTALL_ADD and \
        len(install_job.packages) > 0 and \
        len(install_job.pending_downloads) > 0:
        
        # Use the SMU name to derive the platform and release strings
        smu_list = install_job.packages.split(',')
        pending_downloads = install_job.pending_downloads.split(',')
        
        # Derives the platform and release using the first SMU name.
        platform = get_platform(smu_list[0])
        release = get_release(smu_list[0])
    
        create_download_jobs(db_session, platform, release, pending_downloads, 
            install_job.server_id, install_job.server_directory)
        
    return install_job

@app.route('/hosts/download_dashboard/', methods=['GET', 'POST'])
@login_required
def download_dashboard():
    if not can_install(current_user):
        abort(401)
        
    return render_template('host/download_dashboard.html')

def get_download_job_key(user_id, filename, server_id, server_directory):
    return "{}{}{}{}".format(user_id, filename, server_id, server_directory)

def is_pending_on_download(db_session, filename, server_id, server_directory):
    download_job_key_dict = get_download_job_key_dict()
    download_job_key = get_download_job_key(current_user.id, filename, server_id, server_directory)
    
    if download_job_key in download_job_key_dict:
        download_job = download_job_key_dict[download_job_key]
        # Resurrect the download job
        if download_job is not None and download_job.status == JobStatus.FAILED:
            download_job.status = None
            download_job.status_time = None        
            db_session.commit()
        return True
    
    return False

@app.route('/api/create_download_jobs')
@login_required
def api_create_download_jobs():
    try:
        server_id = request.args.get("server_id")
        server_directory = request.args.get("server_directory")
        smu_list = request.args.get("smu_list").split() 
        pending_downloads = request.args.get("pending_downloads").split() 
    
        # Derives the platform and release using the first SMU name.
        if len(smu_list) > 0 and len(pending_downloads) > 0:
            platform = get_platform(smu_list[0])
            release = get_release(smu_list[0])
          
            create_download_jobs(DBSession(), platform, release, pending_downloads, server_id, server_directory)
    except:
        logger.exception('api_create_download_jobs hit exception')
        return jsonify({'status':'Failed'})
    finally:
        return jsonify({'status':'OK'})
    
"""
Pending downloads is an array of TAR files.
"""
def create_download_jobs(db_session, platform, release, pending_downloads, server_id, server_directory):
    smu_meta = db_session.query(SMUMeta).filter(SMUMeta.platform_release == platform + '_' + release).first()
    if smu_meta is not None:  
        for cco_filename in pending_downloads:
            # If the requested download_file is not in the download table, include it
            if not is_pending_on_download(db_session, cco_filename, server_id, server_directory):
                # Unfortunately, the cco_filename may not conform to the SMU format (i.e. CSC).
                # For example, for CRS, it is possible to have hfr-px-5.1.2.CRS-X-2.tar which contains
                # multiple pie file. Thus, we check if it has a "sp" substring.
                if SP_INDICATOR in cco_filename:
                    software_type_id = smu_meta.sp_software_type_id
                else:
                    software_type_id = smu_meta.smu_software_type_id
            
                download_job = DownloadJob(
                    cco_filename = cco_filename,
                    pid = smu_meta.pid,
                    mdf_id = smu_meta.mdf_id,
                    software_type_id = software_type_id,
                    server_id = server_id,
                    server_directory = server_directory,
                    user_id = current_user.id,
                    created_by = current_user.username)
                
                db_session.add(download_job)

            db_session.commit()
    
def create_install_jobs_for_all_install_actions(db_session, host_id, form):                
    # Create Pre-Upgrade
    new_install_job = create_or_update_install_job(db_session=db_session, host_id=host_id, form=form, \
        install_action=InstallAction.PRE_UPGRADE)
    
    # Create Install Add
    new_install_job = create_or_update_install_job(db_session=db_session, host_id=host_id, form=form, \
        install_action=InstallAction.INSTALL_ADD, dependency=new_install_job.id)
            
    # Create Activate
    new_install_job = create_or_update_install_job(db_session=db_session, host_id=host_id, form=form, \
        install_action=InstallAction.ACTIVATE, dependency=new_install_job.id)
              
    # Create Post-Upgrade
    new_install_job = create_or_update_install_job(db_session=db_session, host_id=host_id, form=form, \
        install_action=InstallAction.POST_UPGRADE, dependency=new_install_job.id)
    
    # Create Install-Commit
    new_install_job = create_or_update_install_job(db_session=db_session, host_id=host_id, form=form, \
        install_action=InstallAction.INSTALL_COMMIT, dependency=new_install_job.id)

def handle_schedule_install_form(request, db_session, hostname, install_job=None):    
    host = get_host(db_session, hostname)
    if host is None:
        abort(404)
         
    return_url = get_return_url(request, 'host_dashboard')       
    form = HostScheduleInstallForm(request.form)
    
    # Retrieves all the install jobs for this host.  This will allow
    # the user to select which install job this install job can depend on.
    install_jobs = db_session.query(InstallJob).filter(InstallJob.host_id == host.id).order_by(InstallJob.scheduled_time.asc()).all()
        
    # Fills the selection
    fill_install_actions(choices=form.install_action.choices, include_ALL=True)
    fill_servers(form.server.choices, host.region.servers)
    fill_servers(form.dialog_server.choices, host.region.servers)
    fill_dependency_from_host_install_jobs(form.dependency.choices, install_jobs, (-1 if install_job is None else install_job.id))
        
    if request.method == 'POST':
        if install_job is not None:
            # In Edit mode, the install_action UI on HostScheduleForm is disabled (not allow to change).
            # Thus, there will be no value returned by form.install_action.data.  So, re-use the existing ones.
            install_action = [ install_job.install_action ]
        else:
            install_action = form.install_action.data
        
        # install_action is a list object which may contain multiple install actions.
        # If 'ALL' is among the selected install actions, do 'ALL' and ignore others
        if InstallAction.ALL in install_action:
            # The ALL install action is available for newly created install job
            if install_job is None:
                create_install_jobs_for_all_install_actions(db_session=db_session, host_id=host.id, form=form)
        else:
            # If only one install_action, accept the selected dependency if any
            if len(install_action) == 1:
                dependency = int(form.dependency.data)        
                create_or_update_install_job(db_session=db_session, host_id=host.id, form=form, 
                    install_action=install_action[0], dependency=dependency, install_job=install_job)
            else:
                # The dependency on each install action is already indicated in the implicit ordering in the selector.
                # If the user selected Pre-Upgrade and Install Add, Install Add (successor) will 
                # have Pre-Upgrade (predecessor) as the dependency.
                dependency = 0              
                for one_install_action in install_action:
                    new_install_job = create_or_update_install_job(db_session=db_session, host_id=host.id, form=form, 
                        install_action=one_install_action, dependency=dependency, install_job=install_job)
                    dependency = new_install_job.id
                    
        return redirect(url_for(return_url, hostname=hostname))
    
    elif request.method == 'GET':
        # Initialize the hidden fields
        form.hidden_server_directory.data = '' 
        form.hidden_pending_downloads.data = ''
        form.hidden_edit.data = install_job is not None

        if install_job is not None:   
            form.install_action.data = install_job.install_action
            form.server.data = install_job.server_id
 
            form.hidden_server_directory.data = '' if install_job.server_directory is None else install_job.server_directory
            form.hidden_pending_downloads.data = '' if install_job.pending_downloads is None else install_job.pending_downloads

            # Form a line separated list for the textarea
            form.software_packages.data = '\n'.join(install_job.packages.split(','))
            form.dependency.data = str(install_job.dependency)
        
            if install_job.scheduled_time is not None:
                form.scheduled_time_UTC.data = \
                get_datetime_string(install_job.scheduled_time)
        
    return render_template('host/schedule_install.html', form=form, \
        host=host, server_time=datetime.datetime.utcnow(), install_job=install_job, return_url=return_url)


def get_host_schedule_install_form(request, host):
    return HostScheduleInstallForm(request.form)

@app.route('/admin_console/', methods=['GET','POST'])
@login_required
def admin_console():
    if current_user.privilege != UserPrivilege.ADMIN:
        abort(401)
     
    db_session = DBSession()   

    smtp_form = SMTPForm(request.form)
    admin_console_form = AdminConsoleForm(request.form)    
    
    smtp_server = get_smtp_server(db_session)
    system_option = SystemOption.get(db_session)
    
    if request.method == 'POST' and \
        smtp_form.validate() and \
        admin_console_form.validate():
 
        if smtp_server is None:
            smtp_server = SMTPServer()
            db_session.add(smtp_server)
      
        smtp_server.server = smtp_form.server.data
        smtp_server.server_port = smtp_form.server_port.data if len(smtp_form.server_port.data) > 0 else None
        smtp_server.sender = smtp_form.sender.data
        smtp_server.use_authentication = smtp_form.use_authentication.data
        smtp_server.username = smtp_form.username.data
        if len(smtp_form.password.data) > 0:
            smtp_server.password = smtp_form.password.data
        smtp_server.secure_connection = smtp_form.secure_connection.data
 
        system_option.inventory_threads = admin_console_form.num_inventory_threads.data
        system_option.install_threads = admin_console_form.num_install_threads.data
        system_option.download_threads = admin_console_form.num_download_threads.data
        system_option.can_schedule = admin_console_form.can_schedule.data
        system_option.can_install = admin_console_form.can_install.data  
        system_option.enable_email_notify = admin_console_form.enable_email_notify.data 
        system_option.enable_inventory = admin_console_form.enable_inventory.data 
        system_option.inventory_hour = admin_console_form.inventory_hour.data 
        system_option.inventory_history_per_host = admin_console_form.inventory_history_per_host.data 
        system_option.download_history_per_user = admin_console_form.download_history_per_user.data
        system_option.install_history_per_host = admin_console_form.install_history_per_host.data
        system_option.total_system_logs = admin_console_form.total_system_logs.data
        system_option.enable_default_host_authentication = admin_console_form.enable_default_host_authentication.data 
        system_option.default_host_username = admin_console_form.default_host_username.data
        if len(admin_console_form.default_host_password.data) > 0:
            system_option.default_host_password = admin_console_form.default_host_password.data
         
        db_session.commit()
        
        return redirect(url_for('home') ) 
    else:
        
        admin_console_form.num_inventory_threads.data = system_option.inventory_threads
        admin_console_form.num_install_threads.data = system_option.install_threads
        admin_console_form.num_download_threads.data = system_option.download_threads
        admin_console_form.can_schedule.data = system_option.can_schedule
        admin_console_form.can_install.data = system_option.can_install
        admin_console_form.enable_email_notify.data = system_option.enable_email_notify
        admin_console_form.enable_inventory.data = system_option.enable_inventory
        admin_console_form.inventory_hour.data = system_option.inventory_hour 
        admin_console_form.inventory_history_per_host.data = system_option.inventory_history_per_host
        admin_console_form.download_history_per_user.data = system_option.download_history_per_user
        admin_console_form.install_history_per_host.data = system_option.install_history_per_host
        admin_console_form.total_system_logs.data = system_option.total_system_logs
        admin_console_form.enable_default_host_authentication.data = system_option.enable_default_host_authentication
        admin_console_form.default_host_username.data = system_option.default_host_username
        
        if smtp_server is not None:
            smtp_form.server.data = smtp_server.server
            smtp_form.server_port.data = smtp_server.server_port
            smtp_form.sender.data = smtp_server.sender
            smtp_form.use_authentication.data = smtp_server.use_authentication
            smtp_form.username.data = smtp_server.username
            smtp_form.secure_connection.data = smtp_server.secure_connection
            
        return render_template('admin/index.html',
            admin_console_form=admin_console_form,
            smtp_form=smtp_form)

@app.route('/hosts/<hostname>/host_dashboard/')
@login_required
def host_dashboard(hostname):
    db_session = DBSession()
    
    host = get_host(db_session, hostname)
    if host is None:
        abort(404)        
    
    return render_template('host/host_dashboard.html', host=host, 
        form=get_host_schedule_install_form(request, host),
        package_states=[ PackageState.ACTIVE_COMMITTED, PackageState.ACTIVE, PackageState.INACTIVE_COMMITTED, PackageState.INACTIVE ])

@app.route('/hosts/delete_all_failed_installs/', methods=['DELETE'])
@login_required
def delete_all_failed_installs():
    if not can_delete_install(current_user):
        abort(401)
        
    return delete_all_installs(status=JobStatus.FAILED)

@app.route('/hosts/delete_all_scheduled_installs/', methods=['DELETE'])
@login_required
def delete_all_scheduled_installs():
    if not can_delete_install(current_user):
        abort(401)
        
    return delete_all_installs()
    
def delete_all_installs(status=None):        
    db_session = DBSession()
    
    try:
        install_jobs = db_session.query(InstallJob).filter(InstallJob.status == status)    
        for install_job in install_jobs:
            db_session.delete(install_job)
            if status == JobStatus.FAILED:
                delete_install_job_dependencies(db_session, install_job)
        
        db_session.commit()    

        return jsonify({'status':'OK'})
    except:
        logger.exception('delete install job hits exception')
        return jsonify({'status':'Failed: check system logs for details'})

@app.route('/hosts/<hostname>/delete_all_failed_installs/', methods=['DELETE'])
@login_required
def delete_all_failed_installs_for_host(hostname):
    if not can_delete_install(current_user):
        abort(401)
        
    return delete_all_installs_for_host(hostname=hostname, status=JobStatus.FAILED)
    
@app.route('/hosts/<hostname>/delete_all_scheduled_installs/', methods=['DELETE'])
@login_required
def delete_all_scheduled_installs_for_host(hostname):
    if not can_delete_install(current_user):
        abort(401)
        
    return delete_all_installs_for_host(hostname)
    
def delete_all_installs_for_host(hostname, status=None):
    if not can_delete_install(current_user):
        abort(401)
        
    db_session = DBSession()

    host = get_host(db_session, hostname)
    if host is None:
        abort(404)
        
    try: 
        install_jobs = db_session.query(InstallJob).filter(InstallJob.host_id == host.id, InstallJob.status == status).all()
        if not install_jobs:
            return jsonify(status="No record fits the delete criteria.")
        
        for install_job in install_jobs:
            db_session.delete(install_job)
            if status == JobStatus.FAILED:
                delete_install_job_dependencies(db_session, install_job)
            
        db_session.commit()
        return jsonify({'status':'OK'})
    except:
        logger.exception('delete install job hits exception')
        return jsonify({'status':'Failed: check system logs for details'})

@app.route('/hosts/<int:id>/delete_install_job/', methods=['DELETE'])
@login_required
def delete_install_job(id):
    if not can_delete_install(current_user):
        abort(401)
        
    db_session = DBSession()
    
    install_job = db_session.query(InstallJob).filter(InstallJob.id == id).first()
    if install_job is None:
        abort(404)
        
    try:
        # Install jobs that are in progress cannot be deleted.
        if install_job.status is None or install_job.status == JobStatus.FAILED:
            db_session.delete(install_job)        
        delete_install_job_dependencies(db_session, install_job)
        
        db_session.commit()
        
        return jsonify({'status':'OK'})

    except:  
        logger.exception('delete install job hits exception')
        return jsonify({'status':'Failed: check system logs for details'})
    

def delete_install_job_dependencies(db_session, install_job):
    dependencies = db_session.query(InstallJob).filter(InstallJob.dependency == install_job.id).all()
    for dependency in dependencies:
        if dependency.status is None:
            db_session.delete(dependency)
        delete_install_job_dependencies(db_session, dependency)

@app.route('/hosts/<hostname>/<table>/session_log/<int:id>/')
@login_required
def host_session_log(hostname, table, id):
    db_session = DBSession()
    
    record = None   
    if table == 'install_job':
        record = db_session.query(InstallJob).filter(InstallJob.id == id).first()
    elif table == 'install_job_history':
        record = db_session.query(InstallJobHistory).filter(InstallJobHistory.id == id).first()
    elif table == 'inventory_job_history':
        record = db_session.query(InventoryJobHistory).filter(InventoryJobHistory.id == id).first()
        
    if record is None:
        abort(404)
       
    file_path = request.args.get('file_path')
    autlogs_file_path = get_autlogs_directory() + file_path

    if not(os.path.isdir(autlogs_file_path) or os.path.isfile(autlogs_file_path)):
        abort(404)

    file_entries = []
    log_content = '\n'
    if os.path.isdir(autlogs_file_path):
        # Returns all files under the requested directory
        file_list = get_file_list(autlogs_file_path)
        for file in file_list:
            file_entries.append(file_path + os.sep + file)
    else:
        # Returns the contents of the requested file
        fo = None
        try:
            fo = io.open(autlogs_file_path, "r", encoding = 'utf-8')
            lines = fo.readlines()

            for line in lines:
                # Trim UNIX ^M characters
                line = line.replace("\r","").replace("\n","")
                if len(line) > 0:
                    log_content += line + '\n'

        finally:
            fo.close()

    return render_template('host/session_log.html', hostname=hostname, table=table, table_id=id, file_entries=file_entries, log_content=log_content, is_file=os.path.isfile(autlogs_file_path))


@app.route('/hosts/<hostname>/<table>/trace/<int:id>/')
@login_required
def host_trace(hostname, table, id):
    db_session = DBSession()
    
    trace = None
    if table == 'inventory_job_history':
        inventory_job = db_session.query(InventoryJobHistory).filter(InventoryJobHistory.id == id).first() 
        trace = inventory_job.trace if inventory_job is not None else None
    elif table == 'install_job':       
        install_job = db_session.query(InstallJob).filter(InstallJob.id == id).first() 
        trace = install_job.trace if install_job is not None else None
    elif table == 'install_job_history':
        install_job = db_session.query(InstallJobHistory).filter(InstallJobHistory.id == id).first()
        trace = install_job.trace if install_job is not None else None
    elif table == 'download_job':
        download_job = db_session.query(DownloadJob).filter(DownloadJob.id == id).first()
        trace = download_job.trace if download_job is not None else None
           
    return render_template('host/trace.html', hostname=hostname, trace=trace)


""" 
Displays the system related logs.
"""  
@app.route('/logs/')
@login_required
def logs():
    db_session = DBSession()           
    logs = db_session.query(Log) \
        .order_by(Log.created_time.desc())

    return render_template('log.html', logs=logs)          

"""
Returns the log trace of a particular log record.
"""
@app.route('/logs/<int:log_id>/trace/')
@login_required
def log_trace(log_id):
    db_session = DBSession()
    
    log = db_session.query(Log).filter(Log.id == log_id).first()
    if log is None:
        abort(404)
    
    return render_template('trace.html', log=log)

@app.route('/failed_software_inventory_list/')
@login_required
def failed_software_inventory_list():
    db_session = DBSession()
    
    inventory_jobs = db_session.query(InventoryJob).filter(InventoryJob.status == JobStatus.FAILED)
    
    return render_template('failed_software_inventory.html', inventory_jobs=inventory_jobs)

@app.route('/api/hosts/')
@login_required
def api_host_list():
    db_session = DBSession()
    
    hosts = get_host_list(db_session)
    return get_host_json(hosts, request)

@app.route('/api/hosts/<hostname>/')
@login_required
def api_host(hostname):
    db_session = DBSession()
    
    host = get_host(db_session, hostname)
    return get_host_json([ host ], request)
    
"""
Allows /api/hosts/search?hostname=%XX% type query
"""
@app.route('/api/hosts/search')
@login_required
def api_host_search_list():
    db_session = DBSession()
    
    criteria='%'
    if len(request.args) > 0:
        criteria = request.args.get('hostname', '')
 
    hosts = db_session.query(Host).filter(Host.hostname.like(criteria)).order_by(Host.hostname.asc()).all()
    return get_host_json(hosts, request)

"""
Returns an array of files on the server repository.  The dictionary contains
['filename'] = file
['is_directory'] = True|False
"""
@app.route('/api/get_server_file_dict/<int:server_id>')
@login_required
def api_get_server_file_dict(server_id):  
    result_list, reachable = get_server_file_dict(server_id, request.args.get("server_directory"))
    if reachable:
        return jsonify(**{'data' : result_list})
    else:
        return jsonify({'status':'Failed'})


"""
Returns an array of dictionaries which contain the file and directory listing
"""
def get_server_file_dict(server_id, server_directory):
    db_session = DBSession()
    server_file_dict = []
    
    server = db_session.query(Server).filter(Server.id == server_id).first()
    if server is not None:
        server_impl = get_server_impl(server)
        server_file_dict = server_impl.get_file_and_directory_dict(server_directory)
    
    return server_file_dict
    
    
@app.route('/api/get_last_successful_install_add_packages/hosts/<int:host_id>')
@login_required
def api_get_last_successful_install_add_packages(host_id):
    result_list = []
    db_session = DBSession()
    install_job_packages = db_session.query(InstallJobHistory.packages). \
        filter((InstallJobHistory.host_id == host_id), 
        and_(InstallJobHistory.install_action == InstallAction.INSTALL_ADD, InstallJobHistory.status == JobStatus.COMPLETED)). \
        order_by(InstallJobHistory.status_time.desc()).first()
    if install_job_packages is not None:
        result_list.append(install_job_packages)
    
    return jsonify(**{'data':result_list})

@app.route('/api/get_past_install_add_packages/hosts/<int:host_id>')
@login_required
def api_get_past_install_add_packages(host_id):
    rows = []
    db_session = DBSession()
    install_jobs = db_session.query(InstallJobHistory). \
        filter((InstallJobHistory.host_id == host_id), 
        and_(InstallJobHistory.install_action == InstallAction.INSTALL_ADD, InstallJobHistory.status == JobStatus.COMPLETED)). \
        order_by(InstallJobHistory.status_time.desc())
        
    for install_job in install_jobs:
        row = {}
        row['packages'] = install_job.packages
        row['status_time'] = install_job.status_time
        row['created_by'] = install_job.created_by
        rows.append(row)
    
    return jsonify(**{'data':rows})

@app.route('/api/get_servers')
@login_required
def api_get_servers():
    result_list = []
    db_session = DBSession()   
    servers = get_server_list(db_session)
    if servers is not None:
        for server in servers:
            result_list.append({ 'server_id': server.id, 'hostname': server.hostname })
    
    return jsonify(**{'data':result_list})

@app.route('/api/get_servers/region/<int:region_id>')
@login_required
def api_get_servers_by_region(region_id):
    result_list = [] 
    db_session = DBSession() 
    region = get_region_by_id(db_session, region_id)
    result_list.append({ 'server_id': '-1', 'hostname': '' })
    if region is not None and len(region.servers) > 0:
        for server in region.servers:
            result_list.append({ 'server_id': server.id, 'hostname': server.hostname })
       
    return jsonify(**{'data':result_list})

@app.route('/api/get_hosts/region/<int:region_id>/role/<role>')
@login_required
def api_get_hosts_by_region(region_id, role):

    rows = []
    db_session = DBSession()    
    hosts = db_session.query(Host). \
        filter(Host.region_id == region_id). \
        order_by(Host.hostname.asc())
        
    if hosts is not None:
        for host in hosts:
            row = {}
            row['hostname'] = host.hostname
            row['roles'] = host.roles
            
            if role.lower() == 'any':
                rows.append(row)
            else:
                # If one of the host roles matches 'role', include it.
                if host.roles is not None:
                    roles = host.roles.split(',')
                    for host_role in roles:
                        if host_role == role:
                            rows.append(row)
                            break
    
    return jsonify(**{'data':rows})

@app.route('/api/get_software/<hostname>')
@login_required
def get_software(hostname):
    if not can_retrieve_software(current_user):
        abort(401)
    
    db_session = DBSession()
    
    host = get_host(db_session, hostname)
    if host is not None:
        host.inventory_job[0].pending_submit = True
        db_session.commit()
        return jsonify({'status':'OK'})
    else:
        return jsonify({'status':'Failed'}) 
 
@app.route('/api/remove_host_password/<hostname>', methods=['POST'])
@login_required   
def api_remove_host_password(hostname):
    if not can_create(current_user):
        abort(401)
    
    db_session = DBSession()
    
    host = get_host(db_session, hostname)
    if host is not None:
        host.connection_param[0].password = ''
        db_session.commit()
        return jsonify({'status':'OK'})
    else:
        return jsonify({'status':'Failed'}) 
    
@app.route('/api/remove_jump_host_password/<hostname>', methods=['POST'])
@login_required   
def api_remove_jump_host_password(hostname):
    if not can_create(current_user):
        abort(401)
    
    db_session = DBSession()
    
    host = get_jump_host(db_session, hostname)
    if host is not None:
        host.password = ''
        db_session.commit()
        return jsonify({'status':'OK'})
    else:
        return jsonify({'status':'Failed'}) 
    
@app.route('/api/check_host_reachability')
@login_required
def check_host_reachability():
    if not can_check_reachability(current_user):
        abort(401)
        
    urls = []   
    # Below information is directly from the page and 
    # may not have been saved yet.
    hostname =  request.args.get('hostname')
    platform = request.args.get('platform')
    host_or_ip = request.args.get('host_or_ip')
    username = request.args.get('username')
    password = request.args.get('password')
    connection_type = request.args.get('connection_type')
    port_number = request.args.get('port_number')
    jump_host_id = request.args.get('jump_host')
    
    # If a jump host exists, create the connection URL
    if int(jump_host_id) > 0:
        db_session = DBSession()
        jump_host = get_jump_host_by_id(db_session=db_session, id=jump_host_id)
        if jump_host is not None:
            url = make_url(connection_type=jump_host.connection_type, username=jump_host.username, \
                password=jump_host.password, host_or_ip=jump_host.host_or_ip, port_number=jump_host.port_number)
            urls.append(url)
    
    db_session = DBSession()
    # The form is in the edit mode and the user clicks Validate Reachability
    # If there is no password specified, try get it from the database.
    if password == '':
        host = get_host(db_session, hostname)
        if host is not None:
            password = host.connection_param[0].password
            
    default_username=None
    default_password=None
    system_option = SystemOption.get(db_session)
    if system_option.enable_default_host_authentication:
        default_username=system_option.default_host_username
        default_password=system_option.default_host_password
                
    url = make_url(connection_type=connection_type, 
        username=username, 
        password=password, 
        host_or_ip=host_or_ip, 
        port_number=port_number,
        default_username=default_username,
        default_password=default_password)
    urls.append(url)
    
    return jsonify({'status':'OK'}) if is_connection_valid(platform, urls) else jsonify({'status':'Failed'})
    
@app.route('/api/check_jump_host_reachability')
@login_required
def check_jump_host_reachability():
    if not can_check_reachability(current_user):
        abort(401)
       
    host_or_ip = request.args.get('host_or_ip')   
    connection_type = request.args.get('connection_type')
    port_number = request.args.get('port_number')
    
    port = 23 # default telnet port
    if len(port_number) > 0:
        port = int(port_number)
    elif connection_type == ConnectionType.SSH:
        port = 22
    
    return jsonify({'status':'OK'}) if is_reachable(host_or_ip, port) else jsonify({'status':'Failed'})
    
@app.route('/api/check_server_reachability')
@login_required
def check_server_reachability():
    if not can_check_reachability(current_user):
        abort(401)
            
    hostname =  request.args.get('hostname')
    server_type = request.args.get('server_type')
    server_url = request.args.get('server_url')
    username = request.args.get('username')
    password = request.args.get('password')
    server_directory = request.args.get('server_directory')
    
    server = Server(hostname=hostname, server_type=server_type, server_url=server_url, 
        username=username, password=password, server_directory=server_directory)
    # The form is in the edit mode and the user clicks Validate Reachability
    # If there is no password specified, try get it from the database.
    if (server_type == ServerType.FTP_SERVER or server_type == ServerType.SFTP_SERVER) and password == '':
        db_session = DBSession()
        server_in_db = get_server(db_session, hostname)
        if server_in_db is not None:
            server.password = server_in_db.password

    server_impl = get_server_impl(server)
    return jsonify({'status':'OK'}) if server_impl.check_reachability() else jsonify({'status':'Failed'}) 

@app.route('/api/validate_cisco_user', methods=['POST'])
@login_required
def validate_cisco_user():   
    if not can_check_reachability(current_user):
        abort(401)
    
    try:
        username = request.form['username']      
        password = request.form['password']
    
        if len(password) == 0:
            password = Preferences.get(DBSession(), current_user.id).cco_password
    
        BSDServiceHandler.get_access_token(username, password)
        return jsonify({'status':'OK'}) 
    except KeyError:
        return jsonify({'status':'Failed'})
    except:
        logger.exception('validate_cisco_user hit exception')
        return jsonify({'status':'Failed'})         

def get_host_json(hosts, request):    
    if hosts is None:
        response = jsonify({'status': 'Not Found'})
        response.status = 404
        return response
        
    hosts_list = []       
    for host in hosts:
        hosts_list.append(host.get_json())
    
    return jsonify(**{'host':hosts_list})

def fill_install_actions(choices, include_ALL=False):
    # Remove all the existing entries
    del choices[:]  
     
    # The install action is listed in implicit ordering.  This ordering
    # is used to formulate the dependency.
    choices.append((InstallAction.PRE_UPGRADE, InstallAction.PRE_UPGRADE))
    choices.append((InstallAction.INSTALL_ADD, InstallAction.INSTALL_ADD))
    choices.append((InstallAction.ACTIVATE, InstallAction.ACTIVATE))
    choices.append((InstallAction.POST_UPGRADE, InstallAction.POST_UPGRADE))
    choices.append((InstallAction.INSTALL_COMMIT, InstallAction.INSTALL_COMMIT))
    
    if include_ALL:
        choices.append((InstallAction.ALL, InstallAction.ALL))
        
def fill_dependencies(choices):
    # Remove all the existing entries
    del choices[:] 
    choices.append((-1, 'None'))  
     
    # The install action is listed in implicit ordering.  This ordering
    # is used to formulate the dependency.
    choices.append((InstallAction.PRE_UPGRADE, InstallAction.PRE_UPGRADE))
    choices.append((InstallAction.INSTALL_ADD, InstallAction.INSTALL_ADD))
    choices.append((InstallAction.ACTIVATE, InstallAction.ACTIVATE))
    choices.append((InstallAction.POST_UPGRADE, InstallAction.POST_UPGRADE))
    choices.append((InstallAction.INSTALL_COMMIT, InstallAction.INSTALL_COMMIT))

def fill_servers(choices, servers):
    # Remove all the existing entries
    del choices[:]
    choices.append((-1, ''))
    
    if len(servers) > 0:
        for server in servers:
            choices.append((server.id, server.hostname))
        
def fill_dependency_from_host_install_jobs(choices, install_jobs, current_install_job_id):
    # Remove all the existing entries
    del choices[:]
    choices.append((-1, 'None'))
    
    for install_job in install_jobs:
        if install_job.id != current_install_job_id:
            choices.append((install_job.id, '%s - %s' % (install_job.install_action, get_datetime_string(install_job.scheduled_time)) ))
        
def fill_jump_hosts(choices):
    # Remove all the existing entries
    del choices[:]
    choices.append((-1, 'None'))
    
    # do not close session as the caller will do it
    db_session = DBSession()
    try:
        hosts = get_jump_host_list(db_session)
        if hosts is not None:
            for host in hosts:
                choices.append((host.id, host.hostname))
    except:
        logger.exception('fill_jump_hosts() hits exception')

def fill_regions(choices):
    # Remove all the existing entries
    del choices[:]
    choices.append((-1, ''))
    
    # do not close session as the caller will do it
    db_session = DBSession()
    try:
        regions = get_region_list(db_session)
        if regions is not None:
            for region in regions:
                choices.append((region.id, region.name))
    except:
        logger.exception('fill_regions() hits exception')
    
"""
Returns the return_url encoded in the parameters
"""
def get_return_url(request, default_url=None):
    url = request.args.get('return_url')
    if url is None:
        url = default_url
    return url

@app.errorhandler(401)
def error_not_authorized(error):
    return render_template('user/not_authorized.html', user=current_user)

@app.errorhandler(404)
def error_not_found(error):
    return render_template('error/not_found.html'), 404   

@app.errorhandler(500)
def catch_server_errors(e):
    logger.exception("Server error!")   

@app.route('/shutdown')
def shutdown_server():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()
    
    return 'Flask has been shutdown'
    
@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session = DBSession()
    db_session.close()

# Setup logging for production.
if not app.debug:
    app.logger.addHandler(logging.StreamHandler()) # Log to stderr.
    app.logger.setLevel(logging.INFO)
 
def can_check_reachability(current_user):
    return current_user.privilege == UserPrivilege.ADMIN or \
        current_user.privilege == UserPrivilege.OPERATOR
        
def can_retrieve_software(current_user):
    return current_user.privilege == UserPrivilege.ADMIN or \
        current_user.privilege == UserPrivilege.OPERATOR
        
def can_install(current_user):
    return current_user.privilege == UserPrivilege.ADMIN or \
        current_user.privilege == UserPrivilege.OPERATOR
        
def can_delete_install(current_user):
    return current_user.privilege == UserPrivilege.ADMIN or \
        current_user.privilege == UserPrivilege.OPERATOR
        
def can_edit_install(current_user):
    return current_user.privilege == UserPrivilege.ADMIN or \
        current_user.privilege == UserPrivilege.OPERATOR
        
def can_edit(current_user):
    return current_user.privilege == UserPrivilege.ADMIN

def can_delete(current_user):
    return can_create(current_user)

def can_create(current_user):
    return current_user.privilege == UserPrivilege.ADMIN
    
def get_host(db_session, hostname):
    return db_session.query(Host).filter(Host.hostname == hostname).first()

def get_host_list(db_session):
    return db_session.query(Host).order_by(Host.hostname.asc()).all()

def get_jump_host_by_id(db_session, id):
    return db_session.query(JumpHost).filter(JumpHost.id == id).first()

def get_jump_host(db_session, hostname):
    return db_session.query(JumpHost).filter(JumpHost.hostname == hostname).first()

def get_jump_host_list(db_session):
    return db_session.query(JumpHost).order_by(JumpHost.hostname.asc()).all()

def get_server(db_session, hostname):
    return db_session.query(Server).filter(Server.hostname == hostname).first()

def get_server_by_id(db_session, id):
    return db_session.query(Server).filter(Server.id == id).first()

def get_server_list(db_session):
    return db_session.query(Server).order_by(Server.hostname.asc()).all()

def get_region(db_session, region_name):
    return db_session.query(Region).filter(Region.name == region_name).first()

def get_region_by_id(db_session, region_id):
    return db_session.query(Region).filter(Region.id == region_id).first()

def get_region_list(db_session):
    return db_session.query(Region).order_by(Region.name.asc()).all()

def get_user(db_session, username):
    return db_session.query(User).filter(User.username == username).first()

def get_user_by_id(db_session, user_id):
    return db_session.query(User).filter(User.id == user_id).first()

def get_user_list(db_session):
    return db_session.query(User).order_by(User.fullname.asc()).all()

def get_smtp_server(db_session):
    return db_session.query(SMTPServer).first()

def event_stream():
    return 'data: testing\n\n'

@app.route('/stream')
def stream():
    return Response(event_stream(),
                          mimetype="text/event-stream")

@app.route('/about')
@login_required
def about():    
    build_date = None
    try:
        build_date = open('build_date', 'r').read()
    except:
        pass
    
    return render_template('about.html', build_date=build_date) 

@app.route('/user_preferences', methods=['GET','POST'])
@login_required
def user_preferences(): 
    db_session = DBSession()
    form = PreferencesForm(request.form)
 
    user = get_user_by_id(db_session, current_user.id)
     
    if request.method == 'POST' and form.validate():
        user.preferences[0].cco_username = form.cco_username.data 
        
        if len(form.cco_password.data) > 0:
            user.preferences[0].cco_password = form.cco_password.data 
        
        # All the checked checkboxes (i.e. platforms and releases to exclude).
        values = request.form.getlist('check') 
        excluded_platform_list = ','.join(values)
        
        preferences = Preferences.get(db_session, current_user.id)
        preferences.excluded_platforms_and_releases = excluded_platform_list
            
        db_session.commit()
        
        return redirect(url_for('home') ) 
    else:
        preferences = user.preferences[0]
        form.cco_username.data = preferences.cco_username
       
    return render_template('csm_client/preferences.html', form=form, platforms_and_releases=get_platforms_and_releases_dict(db_session)) 

def get_platforms_and_releases_dict(db_session):
    excluded_platform_list = []
    preferences = Preferences.get(db_session, current_user.id)
    
    # It is possible that the preferences have not been created yet.
    if preferences is not None and preferences.excluded_platforms_and_releases is not None:
        excluded_platform_list = preferences.excluded_platforms_and_releases.split(',')
    
    rows = []    
    catalog = SMUInfoLoader.get_catalog()
    if len(catalog) > 0:
        for platform in catalog:
            releases = catalog[platform]
            for release in releases:
                row = {}
                row['platform'] = platform
                row['release'] = release
                row['excluded'] = True if platform + '_' + release in excluded_platform_list else False
                rows.append(row)
    else:
        # If get_catalog() failed, populate the excluded platforms and releases
        for platform_and_release in excluded_platform_list:
            pos = platform_and_release.rfind('_')
            if pos > 0:
                row = {}
                row['platform'] = platform_and_release[:pos]
                row['release'] = platform_and_release[pos+1:]
                row['excluded'] = True
                rows.append(row)
            
    return rows
    
@app.route('/get_smu_list/platform/<platform>/release/<release>')
@login_required
def get_smu_list(platform, release):        
    form = DialogServerForm(request.form)
    fill_servers(form.dialog_server.choices, get_server_list(DBSession()))
    
    return render_template('csm_client/get_smu_list.html', form=form, platform=platform, release=release) 

@app.route('/api/get_smu_list/platform/<platform>/release/<release>')
@login_required
def api_get_smu_list(platform, release):    
    smu_loader = SMUInfoLoader(platform, release)
    
    if request.args.get('filter') == 'Optimal':
        return get_smu_or_sp_list(smu_loader.get_optimal_smu_list(), smu_loader.file_suffix)
    else:
        return get_smu_or_sp_list(smu_loader.get_smu_list(), smu_loader.file_suffix)

@app.route('/api/get_sp_list/platform/<platform>/release/<release>')
@login_required
def api_get_sp_list(platform, release):  
    smu_loader = SMUInfoLoader(platform, release)
    
    if request.args.get('filter')  == 'Optimal':
        return get_smu_or_sp_list(smu_loader.get_optimal_sp_list(), smu_loader.file_suffix)
    else:     
        return get_smu_or_sp_list(smu_loader.get_sp_list(), smu_loader.file_suffix)
 
def get_smu_or_sp_list(smu_info_list, file_suffix): 

    file_list = get_file_list(get_repository_directory(), '.' + file_suffix)
    
    rows = []  
    for smu_info in smu_info_list:
        row = {}
        row['ST'] = 'True' if smu_info.name + '.' + file_suffix in file_list else 'False'
        row['package_name'] = smu_info.name + '.' + file_suffix 
        row['posted_date'] = smu_info.posted_date.split()[0]
        row['ddts'] = smu_info.ddts
        row['ddts_url'] = '<a href="' + BUG_SEARCH_URL + smu_info.ddts + '" target="_blank">' + smu_info.ddts + '</a>'
        row['type'] = smu_info.type
        row['description'] = smu_info.description
        row['impact'] = smu_info.impact
        row['functional_areas'] = smu_info.functional_areas
        row['id'] = smu_info.id
        row['name'] = smu_info.name
        row['status'] = smu_info.status
        row['package_bundles'] = smu_info.package_bundles
        row['compressed_image_size'] = smu_info.compressed_image_size
        row['uncompressed_image_size'] = smu_info.uncompressed_image_size
        rows.append(row)
    
    return jsonify( **{'data':rows} )

@app.route('/api/get_smu_details/smu_id/<smu_id>')
@login_required
def api_get_smu_details(smu_id):
    rows = []
    db_session = DBSession()
    
    smu_info = db_session.query(SMUInfo).filter(SMUInfo.id == smu_id).first()
    if smu_info is not None:
        row = {}
        row['id'] = smu_info.id
        row['name'] = smu_info.name
        row['status'] = smu_info.status
        row['type'] = smu_info.type
        row['posted_date'] = smu_info.posted_date
        row['ddts'] = smu_info.ddts
        row['description'] = smu_info.description
        row['functional_areas'] = smu_info.functional_areas
        row['impact'] = smu_info.impact
        row['package_bundles'] = smu_info.package_bundles
        row['compressed_image_size'] = str(smu_info.compressed_image_size)
        row['uncompressed_image_size'] = str(smu_info.uncompressed_image_size)
        row['prerequisites'] = smu_info.prerequisites
        row['supersedes'] = smu_info.supersedes
        row['superseded_by'] = smu_info.superseded_by
        row['composite_DDTS'] = smu_info.composite_DDTS
        row['prerequisites_smu_ids'] = get_smu_ids(db_session, smu_info.prerequisites)
        row['supersedes_smu_ids'] = get_smu_ids(db_session, smu_info.supersedes)
        row['superseded_by_smu_ids'] = get_smu_ids(db_session, smu_info.superseded_by)

        rows.append(row)

    return jsonify( **{'data':rows} )

def get_smu_ids(db_session, smu_name_list):
    smu_ids = []
    smu_names = comma_delimited_str_to_array(smu_name_list)
    for smu_name in smu_names:
        smu_info = db_session.query(SMUInfo).filter(SMUInfo.name == smu_name).first()
        if smu_info is not None:
            smu_ids.append(smu_info.id)
        else:
            smu_ids.append('Unknown')
                  
    return ','.join([id for id in smu_ids])
    

@app.route('/optimize_list')
@login_required
def optimize_list(): 
    return render_template('csm_client/optimize_list.html')

@app.route('/api/check_cisco_authentication/', methods=['POST'])
@login_required
def check_cisco_authentication():
    preferences = Preferences.get(DBSession(), current_user.id) 
    if preferences is not None:
        if not is_empty(preferences.cco_username) and not is_empty(preferences.cco_password):
            return jsonify({'status':'OK'})
    
    return jsonify({'status':'Failed'}) 

@app.route('/api/get_catalog')
@login_required
def api_get_catalog():  
    db_session = DBSession()
    excluded_platform_list = []
    
    preferences = Preferences.get(db_session, current_user.id)  
    if preferences.excluded_platforms_and_releases is not None:
        excluded_platform_list = preferences.excluded_platforms_and_releases.split(',')
        
    rows = []
    
    catalog = SMUInfoLoader.get_catalog()
    for platform in catalog:
        releases = get_filtered_platform_list(platform, catalog[platform], excluded_platform_list)
        if len(releases) > 0:
            row = {}
            row['platform'] = platform
            row['beautified_platform'] = beautify_platform(platform)
            row['releases'] = releases
            rows.append(row)
    
    return jsonify( **{'data':rows} )

def get_filtered_platform_list(platform, releases, excluded_platform_list):
    result_list = []
    for release in releases:
        if platform + '_' + release not in excluded_platform_list:
            result_list.append(release)
            
    return result_list

"""
Given a SMU list, return the ones that are missing in the server repository.
"""
@app.route('/api/get_missing_files_on_server/<int:server_id>')
@login_required
def api_get_missing_files_on_server(server_id):
    rows = []    
    smu_list = request.args.get('smu_list').split() 
    server_directory = request.args.get('server_directory')
   
    server_file_dict, is_reachable = get_server_file_dict(server_id, server_directory)

    # Identify if the SMUs are downloadable on CCO or not. 
    # If they are engineering SMUs, they cannot be downloaded.
    download_info_dict, smu_loader = get_download_info_dict(smu_list)

    if is_reachable:
        for smu_name, cco_filename in download_info_dict.items():
            if not is_smu_on_server_repository(server_file_dict, smu_name):
                # If selected SMU on CCO
                if cco_filename is not None:             
                    rows.append({'smu_entry':smu_name, 'cco_filename': cco_filename, 'is_downloadable':True})
                else:
                    rows.append({'smu_entry':smu_name, 'is_downloadable':False})
    else:
        return jsonify({'status':'Failed'}) 
    
    return jsonify( **{'data':rows} )

def is_smu_on_server_repository(server_file_dict, smu_name):
    for file_info in server_file_dict:
        if not file_info['is_directory'] and file_info['filename'] == smu_name:
            return True
    return False

"""
Given a SMU list, return any missing pre-requisites.  The
SMU entries returned also have the file extension appended.
"""
@app.route('/api/get_missing_prerequisite_list')
@login_required
def api_get_missing_prerequisite_list():    
    hostname = request.args.get('hostname')
    # The SMUs selected by the user to install
    smu_list = request.args.get('smu_list').split()

    prerequisite_list = get_missing_prerequisite_list(smu_list)
    host_packages = get_host_active_packages(hostname)
    
    rows = []
    for smu_name in prerequisite_list:
        # If the missing pre-requisites have not been installed
        # (i.e. not in the Active/Active-Committed), include them.
        if not host_packages_contains(host_packages, smu_name):
            rows.append({'smu_entry':smu_name})
    
    return jsonify( **{'data':rows} )

def host_packages_contains(host_packages, smu_name):
    for package in host_packages:
        if smu_name.replace('.pie','') in package:
            return True
    return False

"""
Returns a list of active/active-committed packages.  The list includes SMU/SP/Packages.
"""
def get_host_active_packages(hostname):
    db_session = DBSession()
    host = get_host(db_session, hostname)
    
    result_list = []       
    if host is not None:
        packages = db_session.query(Package).filter(and_(Package.host_id == host.id, or_(Package.state == PackageState.ACTIVE, Package.state == PackageState.ACTIVE_COMMITTED) )).all()      
        for package in packages:
            result_list.append(package.name)
    
    return result_list

@app.route('/api/optimize_list')
@login_required
def api_optimize_list():
    smu_list = request.args.get('smu_list').split()
    resultant_list, error_list = get_optimize_list(smu_list)
 
    rows = []
    for smu_name in resultant_list:
        rows.append({'smu_entry':smu_name})
    
    return jsonify( **{'data':rows} )

# This route will prompt a file download
@app.route('/download_session_log')
@login_required
def download_session_log():
    return send_file(get_autlogs_directory() + request.args.get('file_path'), as_attachment=True)

@app.route('/download_system_logs')
@login_required
def download_system_logs():
    db_session = DBSession()           
    logs = db_session.query(Log) \
        .order_by(Log.created_time.desc())
        
    contents = ''
    for log in logs:
        contents += get_datetime_string(log.created_time) + '\n'
        contents += log.level + ':' + log.msg + '\n'
        contents += log.trace + '\n'
        contents += '-' * 70 + '\n'
        
    # Create a file which contains the size of the image file.
    log_file = open(get_temp_directory() + 'system_logs', 'w')
    log_file.write(contents)
    log_file.close()  
        
    return send_file(get_temp_directory() + 'system_logs', as_attachment=True)
        
if __name__ == '__main__':    
    app.run(host='0.0.0.0', use_reloader=False, threaded=True, debug=False)