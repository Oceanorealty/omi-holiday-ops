"""Public listing photo URLs, with private administrator history and soft archive."""
import hashlib,ipaddress,json,re
from types import SimpleNamespace
from urllib.parse import urlsplit,urlunsplit
from sqlalchemy.exc import IntegrityError
from app.models import Property,PropertyPhoto
from app.operations.stock import lock_stock
from app.operations.tasks import TaskError,form_token,verify_token
from app.operations.task_plans import record_command

MAX_PHOTOS=100


def photo_url(value):
    value=value.strip()
    try:
        parsed=urlsplit(value);host=parsed.hostname
        if not 1<=len(value)<=2000 or parsed.scheme!='https' or not host or parsed.username or parsed.password or parsed.port not in (None,443):raise ValueError()
        if any(c.isspace() or ord(c)<32 for c in value) or '\\' in value:raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:raise ValueError()
        except ValueError as exc:
            if re.fullmatch(r'[0-9.]+',host) or ':' in host:raise ValueError() from exc
            labels=host.lower().split('.')
            if len(labels)<2 or labels[-1] in ('local','localhost','internal','test','invalid') or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in labels):raise ValueError()
        return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,parsed.query,''))
    except (ValueError,TypeError,AttributeError):raise TaskError('Use a public HTTPS photo URL without login credentials or a private-network address') from None


def rows(db,property_id):
    return db.query(PropertyPhoto).filter_by(property_id=property_id).order_by(PropertyPhoto.position,PropertyPhoto.id).all()


def state(db,prop):
    return {'cover':prop.photo_url,'photos':[{k:getattr(row,k) for k in ('id','url','caption','alt_text','position','active','version')} for row in rows(db,prop.id)]}


def token(db,actor,prop):
    digest=hashlib.sha256(json.dumps(state(db,prop),sort_keys=True).encode()).hexdigest()
    return form_token(actor,SimpleNamespace(id=prop.id,version=0),purpose='property-photos:'+digest)


def save(db,*,actor,property_id,token_value,action,photo_id=None,url='',caption='',alt_text='',note):
    if action not in ('add','edit','archive','restore','cover','up','down'):raise TaskError('Unknown photo action')
    try:
        lock_stock(db);db.expire_all();prop=db.get(Property,property_id)
        if not prop:raise TaskError('Property not found')
        before=state(db,prop);digest=hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest()
        verify_token(token_value,actor,prop.id,purpose='property-photos:'+digest)
        photos=rows(db,prop.id);photo=next((r for r in photos if r.id==photo_id),None)
        if action!='add' and not photo:raise TaskError('Photo not found on this property')
        if action in ('add','edit'):
            clean=photo_url(url);caption=caption.strip();alt_text=alt_text.strip()
            if len(caption)>500 or not 1<=len(alt_text)<=200:raise TaskError('Use a caption up to 500 characters and an image description of 1–200 characters')
            if any(r.id!=photo_id and r.url==clean for r in photos):raise TaskError('This photo URL already exists; restore the archived entry if needed')
            if action=='add':
                if len(photos)>=MAX_PHOTOS:raise TaskError(f'A listing supports {MAX_PHOTOS} retained photos, including archived entries')
                photo=PropertyPhoto(property_id=prop.id,url=clean,caption=caption,alt_text=alt_text,position=max([r.position for r in photos],default=0)+1,actor=actor)
                db.add(photo)
            else:
                if not photo.active:raise TaskError('Restore the photo before editing')
                if prop.photo_url==photo.url:prop.photo_url=clean
                photo.url=clean;photo.caption=caption;photo.alt_text=alt_text;photo.version+=1;photo.actor=actor
        elif action=='archive':
            if not photo.active:raise TaskError('Photo already archived')
            if prop.photo_url==photo.url:raise TaskError('Choose another cover before archiving this cover photo')
            photo.active=False;photo.version+=1;photo.actor=actor
        elif action=='restore':
            if photo.active:raise TaskError('Photo is already active')
            photo.active=True;photo.version+=1;photo.actor=actor
        elif action=='cover':
            if not photo.active:raise TaskError('Restore the photo before selecting a cover')
            prop.photo_url=photo.url
        else:
            active=[r for r in photos if r.active]
            if photo not in active:raise TaskError('Restore the photo before moving it')
            index=active.index(photo);other=index+(-1 if action=='up' else 1)
            if not 0<=other<len(active):raise TaskError('Photo is already at the edge')
            photo.position,active[other].position=active[other].position,photo.position
            photo.version+=1;active[other].version+=1
        db.flush()
        record_command(db,token_value,'property-photos',actor,note,{'property_id':prop.id,'action':action,'photo_id':photo.id,'before':before,'after':state(db,prop)})
        db.commit();return photo.id
    except IntegrityError:
        db.rollback();raise TaskError('Photo request already saved; reload before trying again') from None
    except Exception:db.rollback();raise


def public_gallery(db,property_id):
    return [{'url':r.url,'caption':r.caption,'alt_text':r.alt_text} for r in rows(db,property_id) if r.active]


def parse_photo_manifest(content):
    try:
        if not isinstance(content,bytes) or len(content)>524288:raise ValueError()
        data=json.loads(content.decode('utf-8'))
        if not isinstance(data,dict) or set(data)!={'schema_version','property_id','source_url','photos'}:raise ValueError()
        if data['schema_version']!=1 or type(data['property_id']) is not int or data['property_id']<1:raise ValueError()
        if not isinstance(data['source_url'],str):raise ValueError()
        data['source_url']=photo_url(data['source_url'])
        if not isinstance(data['photos'],list) or not 1<=len(data['photos'])<=MAX_PHOTOS:raise ValueError()
        seen=set();clean=[]
        for item in data['photos']:
            if not isinstance(item,dict) or set(item)!={'url','caption','alt_text'}:raise ValueError()
            if any(not isinstance(item[k],str) for k in item):raise ValueError()
            url=photo_url(item['url'])
            if re.search(r'(?:^|[?&])(?:access_token|token|signature|credential|password|key)=',url,re.I):raise ValueError()
            caption=item['caption'].strip();alt=item['alt_text'].strip()
            if url in seen or len(caption)>500 or not 1<=len(alt)<=200:raise ValueError()
            seen.add(url);clean.append({'url':url,'caption':caption,'alt_text':alt})
        return {**data,'photos':clean}
    except (ValueError,TypeError,KeyError,AttributeError,UnicodeError):
        raise TaskError('Use a UTF-8 manifest with an exact property ID/source URL and 1–100 unique public HTTPS photos; no credentials or extra fields') from None


def import_photos(db,*,actor,property_id,token_value,content,note):
    manifest=parse_photo_manifest(content)
    if manifest['property_id']!=property_id:raise TaskError('Manifest property ID does not match this gallery')
    try:
        lock_stock(db);db.expire_all();prop=db.get(Property,property_id)
        if not prop or prop.published:raise TaskError('Bulk imports are limited to unpublished properties')
        if not prop.listing_url or photo_url(prop.listing_url)!=manifest['source_url']:
            raise TaskError('Manifest source URL must match the saved listing source exactly')
        before=state(db,prop);digest=hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest()
        verify_token(token_value,actor,prop.id,purpose='property-photos:'+digest)
        existing=rows(db,property_id);by_url={p.url:p for p in existing};pending=[]
        for item in manifest['photos']:
            previous=by_url.get(item['url'])
            if previous and not previous.active:raise TaskError('A manifest photo is archived; restore it explicitly first')
            if not previous:pending.append(item)
        if len(existing)+len(pending)>MAX_PHOTOS:raise TaskError(f'A listing supports {MAX_PHOTOS} retained photos, including archived entries')
        if not pending:
            db.rollback();return {'added':0,'already_present':len(manifest['photos'])}
        position=max([p.position for p in existing],default=0)
        for index,item in enumerate(pending,1):db.add(PropertyPhoto(property_id=property_id,position=position+index,actor=actor,**item))
        db.flush()
        record_command(db,token_value,'property-photos',actor,note,{'property_id':property_id,'action':'import',
            'source_url':manifest['source_url'],'added':len(pending),'already_present':len(manifest['photos'])-len(pending),
            'before':before,'after':state(db,prop)})
        db.commit();return {'added':len(pending),'already_present':len(manifest['photos'])-len(pending)}
    except IntegrityError:
        db.rollback();raise TaskError('Photo request already saved; reload before trying again') from None
    except Exception:db.rollback();raise
