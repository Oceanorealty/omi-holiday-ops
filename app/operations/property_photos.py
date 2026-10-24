"""Public listing photo URLs, with private administrator history and soft archive."""
import hashlib,ipaddress,json,re
from types import SimpleNamespace
from urllib.parse import urlsplit,urlunsplit
from sqlalchemy.exc import IntegrityError
from app.models import Property,PropertyPhoto
from app.operations.stock import lock_stock
from app.operations.tasks import TaskError,form_token,verify_token
from app.operations.task_plans import record_command


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
                if len(photos)>=50:raise TaskError('A listing supports 50 retained photos, including archived entries')
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
