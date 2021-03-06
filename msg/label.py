"""
creates labels as config'd for initial startup,
handles labeling automation as scheduled
"""
from __future__ import print_function
import re
import base64
import email
from datetime import datetime, timedelta

from auth import auth
from contacts import contacts
from log import log
from msg.utils import agency_slug, user_input, error_info

from config import config


att_exts = config.data["msg"]["label"]["att_exts"]
statuses = config.data["msg"]["label"]["statuses"]
maxResults = config.data["msg"]["label"]["maxResults"]
is_sender_check = config.data["msg"]["label"]["is_sender_check"]

service = auth.get_service()
contacts_by_agency = contacts.get_contacts_by_agency()
agencies = [agency for agency in list(contacts_by_agency.keys())]
slugs = [agency_slug(agency) for agency in agencies]
labels = service.users().labels().list(userId='me').execute()['labels']
agency_label_ids = [x['id'] for x in labels if 'agency' in x['name']]


def msgs_job(msgs=None, date=None):
    """
    control function to handle label automation
    """
    if not msgs:
        msgs = select_unlabeled_msgs(date=date)
    msg_label_queue = []
    for msg in msgs:
        msg_label_queue.append(check_labels(msg))
    update_labels(msg_label_queue)


def select_unlabeled_msgs(date=None):
    """
    this is maybe misnamed.
    selects all messages after specified date (at midnight).
    defaults to today's messages
    e.g. date: datetime.datetime.strptime('2018/04/13','%Y/%m/%d')
    """
    if not date:
        date = datetime.now() - timedelta(days=1)
    date = date.strftime('%Y/%m/%d')
    query = 'after:' + date
    response = service.users().messages().list(userId='me',q=query,maxResults=maxResults).execute()
    messages = response.get('messages', [])
    while 'nextPageToken' in response:
        page_token = response['nextPageToken']
        response = service.users().messages().list(userId='me',q=query,maxResults=maxResults,pageToken=page_token).execute()
        messages.extend(response.get('messages', []))
    return messages


def check_labels(msg):
    """
    given a message, checks:
    - request status
    - agency name
    ... and returns dict for labeling
    """
    try:
        msg = service.users().messages().get(
            id=msg['id'], userId='me'
        ).execute()
    except Exception as e:
        print(error_info(e))
    req_status = check_req_status(msg)
    agency = check_agency_status(msg)
    return {'msg': msg, 'req_status': req_status, 'agency': agency}


def is_sender_email(email):
    domain_check = is_sender_check.get("domain")
    email_check = is_sender_check.get("email")
    if domain_check:
        return email.split('@')[-1] == domain_check
    elif email_check:
        return email.lower() == email_check.lower()
    else:
        raise ValueError(
            "Configuration is missing an `email` or `domain` is_sender_check!"
        )


def check_req_status(msg):
    """
    for incoming messages, checks if
    it has an attachment or not,
    then returns as '*attachment' or '*responded'
    """
    em_from = [x for x in msg['payload']['headers']
               if x['name'] == 'From'][0]['value']
    # hard coded check to determine if a non-sender is on the thread
    # https://github.com/bettergov/foiamail/issues/53
    if is_sender_email(em_from):
        return
    if get_atts(msg):
        return '*attachment'
    return '*responded'


def get_atts(msg):
    """
    scans a message's attachments
    and checks if atts have acceptable extensions
    as configured.
    returns accepted atts
    """
    # list len evals to bool
    atts = []
    if 'parts' in list(msg['payload'].keys()):
        for part in msg['payload']['parts']:
            if 'filename' in list(part.keys()) and \
                    part['filename'].split('.')[-1].lower() in att_exts:
                atts.append(part)
    return atts


def check_agency_status(msg):
    """
    checks to see if message contains an agency slug
    (i.e., char string between two # hashtags)
    and, if so, looks up to see what if any agency it belongs to.
    returns agency name
    """
    slug = check_agency_hashtag(msg)
    # sender_agency = check_sender_agency(msg)
    return lookup_agency_by_slug(slug)


def lookup_agency_by_slug(slug):
    """
    returns the name of the agency
    belonging to the specified slug,
    if any match
    """
    candidates = [x for x in agencies if x.replace(' ', '') == slug]
    if candidates:
        return candidates[0]


def check_sender_agency(msg):
    """
    deprecated.
    originally designed to help lookup the agency by the sender.
    this is problematic because occasionally a contact sends on behalf of multiple agencies.
    keeping this code for reference but it's not advisable to implement,
    i.e. could result in false matches.
    """
    return
    # todo: check for multiple matches ie double agents
    sender = [x for x in msg['payload']['headers']
              if x['name'] == 'From'][0]['value']
    matching_agencies = [
        agency for agency in contacts_by_agency
        if sender in contacts_by_agency[agency]
    ]
    if matching_agencies:
        return matching_agencies[0]


def check_agency_hashtag(msg):
    """
    reads messages, scanning for hashtag-delimited slugs.
    supports multipart and non-multipart messages
    """
    msg = service.users().messages().get(
        id=msg['id'], userId='me', format='raw'
    ).execute()
    body = base64.urlsafe_b64decode(msg['raw'].encode('ASCII'))
    try:
        em = email.message_from_string(body)
    except TypeError:
        em = email.message_from_string(body.decode("utf-8"))

    if em.get_content_maintype() == 'multipart':
        match = recursive_match_scan(em)
    else:
        match = split_and_check(em.get_payload())
    return match


def recursive_match_scan(em):
    """
    i don't know how to properly do recursion
    but it seems to make sense to start the function
    at the point where you know it's multipart
    """
    for part in em.get_payload():
        if part.get_content_maintype() == 'multipart':
            match = recursive_match_scan(part)
        else:
            match = split_and_check(part.get_payload())
        if match:
            return match


def split_and_check(text):
    """
    given a scannable string,
    looks for hashtag-delimited text
    and returns it
    """
    # strip out newlines, they will confuse the b64 check
    if "\n" in text or "\r" in text:
        text = re.sub("[\n\r]+", "", text)
    # check to see if this could be B64 encoded... not sure why we
    # really need to do this, though ... it ruins label extraction
    # when it goes wrong, though.
    b64_re = r"^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)?$"
    if re.match(b64_re, text):
        try:
            text = base64.urlsafe_b64decode(text)
        except:
            pass
    try:
        chunks = text.split('#')
    except TypeError:
        try:
            chunks = text.decode('utf-8').split('#')
        # if we explode and end up here, we have some kind of
        # a binary format or non-UTF8
        except UnicodeDecodeError:
            return None
    for chunk in chunks:
        if '#' + chunk + '#' in slugs:
            return chunk


def update_labels(msg_queue):
    """
    labels messages by
    - agency
    - status
    as specified
    """
    for x in msg_queue:
        try:
            if x['agency']:
                label_agency(x['msg'], x['agency'])
            else:
                if not get_thread_agency_label(x['msg']):
                    label_agency(x['msg'], '*unidentified')
            if x['req_status']:
                label_status(x['msg'], x['req_status'])
            print('labels', x['msg']['id'], x['agency'], x['req_status'])
            #log.log_data('label',[{'msg_id':msg['id'],'agency':x['agency'] if x['agency'] else 'unidentified','status':x['req_status']}])
        except Exception as e:
            print(error_info(e))


def label_agency(msg, agency):
    """
    specifies agency msg should get (or *unidentified)
    and applies it
    """
    if agency == '*unidentified':
        label_id = lookup_label('*unidentified')
    else:
        # see https://github.com/mattkiefer/gm/issues/1
        label_id = lookup_label('agency/' + agency)
    # TODO 2nd check if agency lookup
    if label_id:
        service.users().messages().modify(userId='me', id=msg['id'], body={
            "addLabelIds": [label_id]}).execute()


def label_status(msg, status):
    """
    specifies status label msg should get
    and applies it
    """
    status_label = lookup_label(status)
    # TODO 3rd check if agency assigned,
    # and if status is correct
    # then step through and see if it assigns
    #import ipdb; ipdb.set_trace()
    if status_label:
        service.users().messages().modify(userId='me', id=msg['id'], body={
            "addLabelIds": [status_label]}).execute()


def get_thread_agency_label(msg):
    """
    checks if thread has agency label
    on any messages,
    if so, returns it
    """
    t = service.users().threads().get(
        userId='me', id=msg['threadId']
    ).execute()
    for m in t['messages']:
        for lid in m['labelIds']:
            if lid not in agency_label_ids:
                continue
            return [
                label['name'] for label in labels
                if label['id'] == lid
            ][0]


def lookup_label(label_text):
    """
    returns the label id
    given the label name
    for api lookup purposes
    """
    #matches = [label for label in labels if label['name'].replace(' ','') == label_text]
    matches = [label for label in labels if label['name'] == label_text]
    if matches:
        return matches[0]['id']


def delete_labels(label_ids=None):
    """
    deletes labels
    """
    if not label_ids:
        label_ids = []
        dal = user_input(
            'delete ALL user labels? this is a first-time setup thing [y/N]: '
        )
        if dal.lower() == 'y':
            labels = service.users().labels().list(userId='me').execute()
            print(labels)
            label_ids = [
                x['id'] for x in labels['labels']
                if x['type'] == 'user'
            ]
    for label_id in label_ids:
        print('deleting label', label_id)
        # TODO comment out
        service.users().labels().delete(userId='me', id=label_id).execute()


def create_labels(labels=None):
    """
    creates labels based on
    - agencies (defined by contacts)
    - statuses (defined in configs)
    """
    if not labels:
        labels = []
        labels += ['agency']
        # see https://github.com/mattkiefer/gm/issues/1
        labels += ['agency/' + agency for agency in agencies]
        labels += statuses
    for label in labels:
        print('creating label', label)
        try:
            service.users().labels().create(
                userId='me', body=make_label(label)
            ).execute()
        except Exception as e:
            print("Skipping label %s: %s" % (label, e))


def make_label(label_text):
    """
    returns a label object
    conforming to api specs
    given a name
    """
    return {
        'messageListVisibility': 'show',
        'name': label_text,
        'labelListVisibility': 'labelShow'
    }
