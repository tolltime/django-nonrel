import datetime
import os

from django.conf import settings
from django.db.models.fields import Field
from django.core.files.base import File, ContentFile
from django.core.files.storage import default_storage
from django.core.files.images import ImageFile, get_image_dimensions
from django.core.files.uploadedfile import UploadedFile
from django.utils.functional import curry
from django.db.models import signals
from django.utils.encoding import force_unicode, smart_str
from django.utils.translation import ugettext_lazy, ugettext as _
from django import forms
from django.db.models.loading import cache

class FieldFile(File):
    def __init__(self, instance, field, name):
        self.instance = instance
        self.field = field
        self.storage = field.storage
        self._name = name or u''
        self._closed = False

    def __eq__(self, other):
        # Older code may be expecting FileField values to be simple strings.
        # By overriding the == operator, it can remain backwards compatibility.
        if hasattr(other, 'name'):
            return self.name == other.name
        return self.name == other

    # The standard File contains most of the necessary properties, but
    # FieldFiles can be instantiated without a name, so that needs to
    # be checked for here.

    def _require_file(self):
        if not self:
            raise ValueError("The '%s' attribute has no file associated with it." % self.field.name)

    def _get_file(self):
        self._require_file()
        if not hasattr(self, '_file'):
            self._file = self.storage.open(self.name, 'rb')
        return self._file
    file = property(_get_file)

    def _get_path(self):
        self._require_file()
        return self.storage.path(self.name)
    path = property(_get_path)

    def _get_url(self):
        self._require_file()
        return self.storage.url(self.name)
    url = property(_get_url)

    def _get_size(self):
        self._require_file()
        return self.storage.size(self.name)
    size = property(_get_size)

    def open(self, mode='rb'):
        self._require_file()
        return super(FieldFile, self).open(mode)
    # open() doesn't alter the file's contents, but it does reset the pointer
    open.alters_data = True

    # In addition to the standard File API, FieldFiles have extra methods
    # to further manipulate the underlying file, as well as update the
    # associated model instance.

    def save(self, name, content, save=True):
        name = self.field.generate_filename(self.instance, name)
        self._name = self.storage.save(name, content)
        setattr(self.instance, self.field.name, self.name)

        # Update the filesize cache
        self._size = len(content)

        # Save the object because it has changed, unless save is False
        if save:
            self.instance.save()
    save.alters_data = True

    def delete(self, save=True):
        # Only close the file if it's already open, which we know by the
        # presence of self._file
        if hasattr(self, '_file'):
            self.close()
            del self._file
            
        self.storage.delete(self.name)

        self._name = None
        setattr(self.instance, self.field.name, self.name)

        # Delete the filesize cache
        if hasattr(self, '_size'):
            del self._size

        if save:
            self.instance.save()
    delete.alters_data = True

    def __getstate__(self):
        # FieldFile needs access to its associated model field and an instance
        # it's attached to in order to work properly, but the only necessary
        # data to be pickled is the file's name itself. Everything else will
        # be restored later, by FileDescriptor below.
        return {'_name': self.name, '_closed': False}

class FileDescriptor(object):
    def __init__(self, field):
        self.field = field

    def __get__(self, instance=None, owner=None):
        if instance is None:
            raise AttributeError, "%s can only be accessed from %s instances." % (self.field.name(self.owner.__name__))
        file = instance.__dict__[self.field.name]
        if not isinstance(file, FieldFile):
            # Create a new instance of FieldFile, based on a given file name
            instance.__dict__[self.field.name] = self.field.attr_class(instance, self.field, file)
        elif not hasattr(file, 'field'):
            # The FieldFile was pickled, so some attributes need to be reset.
            file.instance = instance
            file.field = self.field
            file.storage = self.field.storage
        return instance.__dict__[self.field.name]

    def __set__(self, instance, value):
        instance.__dict__[self.field.name] = value

class FileField(Field):
    attr_class = FieldFile

    def __init__(self, verbose_name=None, name=None, upload_to='', storage=None, **kwargs):
        for arg in ('primary_key', 'unique'):
            if arg in kwargs:
                raise TypeError("'%s' is not a valid argument for %s." % (arg, self.__class__))

        self.storage = storage or default_storage
        self.upload_to = upload_to
        if callable(upload_to):
            self.generate_filename = upload_to

        kwargs['max_length'] = kwargs.get('max_length', 100)
        super(FileField, self).__init__(verbose_name, name, **kwargs)

    def get_internal_type(self):
        return "FileField"

    def get_db_prep_lookup(self, lookup_type, value):
        if hasattr(value, 'name'):
            value = value.name
        return super(FileField, self).get_db_prep_lookup(lookup_type, value)

    def get_db_prep_value(self, value):
        "Returns field's value prepared for saving into a database."
        # Need to convert File objects provided via a form to unicode for database insertion
        if value is None:
            return None
        return unicode(value)

    def contribute_to_class(self, cls, name):
        super(FileField, self).contribute_to_class(cls, name)
        setattr(cls, self.name, FileDescriptor(self))
        signals.post_delete.connect(self.delete_file, sender=cls)

    def delete_file(self, instance, sender, **kwargs):
        file = getattr(instance, self.attname)
        # If no other object of this type references the file,
        # and it's not the default value for future objects,
        # delete it from the backend.
        if file and file.name != self.default and \
            not sender._default_manager.filter(**{self.name: file.name}):
                file.delete(save=False)
        elif file:
            # Otherwise, just close the file, so it doesn't tie up resources.
            file.close()

    def save_file(self, new_data, new_object, original_object, change, rel,
                  save=True):
        upload_field_name = self.name + '_file'
        if new_data.get(upload_field_name, False):
            if rel:
                file = new_data[upload_field_name][0]
            else:
                file = new_data[upload_field_name]

            # Backwards-compatible support for files-as-dictionaries.
            # We don't need to raise a warning because the storage backend will
            # do so for us.
            try:
                filename = file.name
            except AttributeError:
                filename = file['filename']
            filename = self.get_filename(filename)

            getattr(new_object, self.attname).save(filename, file, save)

    def get_directory_name(self):
        return os.path.normpath(force_unicode(datetime.datetime.now().strftime(smart_str(self.upload_to))))

    def get_filename(self, filename):
        return os.path.normpath(self.storage.get_valid_name(os.path.basename(filename)))

    def generate_filename(self, instance, filename):
        return os.path.join(self.get_directory_name(), self.get_filename(filename))

    def save_form_data(self, instance, data):
        if data and isinstance(data, UploadedFile):
            getattr(instance, self.name).save(data.name, data, save=False)

    def formfield(self, **kwargs):
        defaults = {'form_class': forms.FileField}
        # If a file has been provided previously, then the form doesn't require
        # that a new file is provided this time.
        # The code to mark the form field as not required is used by
        # form_for_instance, but can probably be removed once form_for_instance
        # is gone. ModelForm uses a different method to check for an existing file.
        if 'initial' in kwargs:
            defaults['required'] = False
        defaults.update(kwargs)
        return super(FileField, self).formfield(**defaults)

class ImageFieldFile(ImageFile, FieldFile):
    def save(self, name, content, save=True):
        # Repopulate the image dimension cache.
        self._dimensions_cache = get_image_dimensions(content)

        # Update width/height fields, if needed
        if self.field.width_field:
            setattr(self.instance, self.field.width_field, self.width)
        if self.field.height_field:
            setattr(self.instance, self.field.height_field, self.height)

        super(ImageFieldFile, self).save(name, content, save)

    def delete(self, save=True):
        # Clear the image dimensions cache
        if hasattr(self, '_dimensions_cache'):
            del self._dimensions_cache
        super(ImageFieldFile, self).delete(save)

class ImageField(FileField):
    attr_class = ImageFieldFile

    def __init__(self, verbose_name=None, name=None, width_field=None, height_field=None, **kwargs):
        self.width_field, self.height_field = width_field, height_field
        FileField.__init__(self, verbose_name, name, **kwargs)

    def formfield(self, **kwargs):
        defaults = {'form_class': forms.ImageField}
        defaults.update(kwargs)
        return super(ImageField, self).formfield(**defaults)
