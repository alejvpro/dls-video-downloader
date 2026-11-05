"""DLS Pushover transport: private config, fixed HTTPS endpoint, no YouTube IO."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import subprocess
import sys
from urllib import request, error, parse

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path.home()/'.config/dls-video-downloader/pushover.json'
ENDPOINT = 'https://api.pushover.net/1/messages.json'
RESPONSE_LIMIT = 16384
CODES = {'http_rejected','provider_rejected','provider_temporary','network_uncertain',
         'invalid_response','worker_failed','worker_timeout','worker_launch_failed','interrupted'}


class ConfigurationError(ValueError):
    """Only fixed safe error codes may cross the credential boundary."""


@dataclass(repr=False)
class Credentials:
    token: str = field(repr=False)
    user: str = field(repr=False)
    device: str | None = field(default=None,repr=False)


def load_credentials(path=DEFAULT_CONFIG):
    """Explicit execution only. Never read the reserved temporary source file."""
    path=Path(os.path.abspath(Path(path).expanduser()))
    if path==Path.home()/'pushover.txt':raise ConfigurationError('reserved_credential_source')
    if path.is_relative_to(PROJECT):raise ConfigurationError('credentials_must_be_outside_repository')
    directory_fd=None;fd=None
    try:
        directory_fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        resolved_parent=path.parent.resolve()
        if resolved_parent.is_relative_to(PROJECT) or (resolved_parent==Path.home().resolve() and path.name=='pushover.txt'):
            raise ConfigurationError('credentials_must_be_outside_repository')
        parent=os.fstat(directory_fd)
        if parent.st_uid!=os.getuid() or stat.S_IMODE(parent.st_mode)!=0o700:
            raise ConfigurationError('credentials_permissions')
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory_fd)
        metadata=os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode) not in (0o600,0o400) or metadata.st_nlink!=1:
            raise ConfigurationError('credentials_permissions')
        with os.fdopen(fd,'rb') as stream:
            fd=None;raw=stream.read(4097)
        if len(raw)>4096:raise ConfigurationError('credentials_invalid')
        data=json.loads(raw)
        if not isinstance(data,dict) or set(data)-{'token','user','device'}:raise ConfigurationError('credentials_invalid')
        if any(not isinstance(data.get(k),str) or not re.fullmatch('[A-Za-z0-9]{30}',data[k]) for k in ('token','user')):
            raise ConfigurationError('credentials_invalid')
        device=data.get('device')
        if device is not None and (not isinstance(device,str) or not re.fullmatch('[A-Za-z0-9_-]{1,25}',device)):
            raise ConfigurationError('credentials_invalid')
        return Credentials(data['token'],data['user'],device)
    except ConfigurationError:raise
    except FileNotFoundError:raise ConfigurationError('credentials_missing') from None
    except (OSError,ValueError,TypeError,KeyError):raise ConfigurationError('credentials_invalid') from None
    finally:
        if fd is not None:os.close(fd)
        if directory_fd is not None:os.close(directory_fd)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def send(payload):
    """Worker only: bounded response and fixed error codes; discard provider text."""
    body={k:payload[k] for k in ('token','user','title','message')}
    if payload.get('device'):body['device']=payload['device']
    body['priority']='0'  # All categories use ordinary priority; no emergency retry.
    req=request.Request(ENDPOINT,data=parse.urlencode(body).encode('utf-8'),method='POST',
                        headers={'Content-Type':'application/x-www-form-urlencoded'})
    try:
        opener=request.build_opener(NoRedirect(),request.HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(req,timeout=10) as response:
            status=response.status;raw=response.read(RESPONSE_LIMIT+1)
        if len(raw)>RESPONSE_LIMIT:return dict(outcome='uncertain',error='invalid_response')
        data=json.loads(raw)
        if status==200 and isinstance(data,dict) and type(data.get('status')) is int and data['status']==1:
            result=dict(outcome='accepted',http_status=status)
            rid=data.get('request')
            if isinstance(rid,str) and re.fullmatch('[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',rid):
                result['provider_request_id']=rid
            return result
        rejected=isinstance(data,dict) and type(data.get('status')) is int and data['status']==0
        return dict(outcome='rejected' if rejected else 'uncertain',
                    error='provider_rejected' if rejected else 'invalid_response',http_status=status)
    except error.HTTPError as exc:
        return dict(outcome='rejected' if 300<=exc.code<500 else 'retryable',
                    error='http_rejected' if 300<=exc.code<500 else 'provider_temporary',http_status=exc.code)
    except (ValueError,UnicodeError):return dict(outcome='uncertain',error='invalid_response')
    except Exception:return dict(outcome='uncertain',error='network_uncertain')


def deliver(credentials,alert,timeout,lock_fd):
    """Bound whole request in a killable child; credentials travel only on stdin."""
    payload=dict(token=credentials.token,user=credentials.user,device=credentials.device,
                 title=alert['title'],message=alert['message'])
    argv=[sys.executable,'-B','-m','dls_video_downloader.pushover']
    try:
        process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                 start_new_session=True,pass_fds=(lock_fd,))
    except OSError:return dict(outcome='retryable',error='worker_launch_failed')
    try:
        raw,_=process.communicate(json.dumps(payload).encode(),timeout=timeout)
        if process.returncode or len(raw)>4096:return dict(outcome='uncertain',error='worker_failed')
        result=json.loads(raw)
        if not isinstance(result,dict) or result.get('outcome') not in ('accepted','retryable','rejected','uncertain'):
            return dict(outcome='uncertain',error='worker_failed')
        safe={'outcome':result['outcome']}
        if result.get('error') in CODES:safe['error']=result['error']
        if type(result.get('http_status')) is int and 100<=result['http_status']<=599:safe['http_status']=result['http_status']
        rid=result.get('provider_request_id')
        if isinstance(rid,str) and re.fullmatch('[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',rid):safe['provider_request_id']=rid
        return safe
    except subprocess.TimeoutExpired:return dict(outcome='uncertain',error='worker_timeout')
    except KeyboardInterrupt:return dict(outcome='uncertain',error='interrupted')
    except Exception:return dict(outcome='uncertain',error='worker_failed')
    finally:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=5)
        for stream in (process.stdin,process.stdout):
            if stream is not None:stream.close()


if __name__=='__main__':
    # Never print exceptions, input or provider bodies, even on malformed input.
    try:
        raw=sys.stdin.buffer.read(8193)
        result=send(json.loads(raw)) if len(raw)<=8192 else dict(outcome='uncertain',error='worker_failed')
    except BaseException:result=dict(outcome='uncertain',error='worker_failed')
    sys.stdout.write(json.dumps(result))
