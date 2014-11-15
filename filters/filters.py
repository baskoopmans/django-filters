# coding: utf-8

import urllib
import math

from django.core.exceptions import ImproperlyConfigured
from django.core.urlresolvers import resolve, reverse_lazy
from django.http import Http404
from django.utils.translation import ugettext as _
from django.template import RequestContext, Context
from django.template.loader import render_to_string
from django.db.models import Count, Min, Max

from djcommon.decorators import cached
from djcommon.helpers import uniqify_list, construct_object

import logging
logger = logging.getLogger(__name__)


class FilterSet(object):
    def __init__(self, queryset, request, view, filters=[]):
        self.original_queryset = queryset
        self.queryset = queryset
        self.request = request
        self.view = view
        self.selected_choices = {}
        self.filter_mapper = {}
        self.filters = []
        logger.info('Initializing filterset %s' % self.filters)

        for filter in filters:
            filter_kwargs = {'queryset': self.queryset, 'request': request, 'view': self.view}
            if type(filter) == str:
                constructed_filter = construct_object(filter, **filter_kwargs)
            else:
                constructed_filter = filter(**filter_kwargs)

            self.filters.append(constructed_filter)
            self.filter_mapper[constructed_filter.name] = constructed_filter

            if constructed_filter.is_active:
                self.queryset = constructed_filter.get_results()

    def __iter__(self):
        for obj in self.queryset:
            yield obj

    @property
    def active_filters(self):
        return [x for x in self.filters if x.is_active]

    @property
    def visible_filters(self):
        return [x for x in self.filters if x.is_visible]

    @property
    def filter_names(self):
        return [x.name for x in self.filters]

    @property
    def active_filter_names(self):
        return [x.name for x in self.active_filters]

    def get_filter(self, name):
        return self.filter_mapper.get(name, None)

    @property
    def meta_noindex(self):
        """
        Check if the filter combination can be indexed.

        If an active filter is set no_index = True
        If request has GET parameters
        """
        test = [x.meta_noindex for x in self.active_filters]
        logger.info('META_NOINDEX %s' % test)
        return True if True in test or self.request.GET else False

    @property
    def rel_canonical(self):
        return self.request.path if self.request.GET else None

    def get_extra_context_data(self):
        get_extra_context_data = {}
        for filter in self.active_filters:
            get_extra_context_data.update(filter.get_extra_context_data())
        return get_extra_context_data


    def get_name(self):
        selected_choices = self.filter_mapper['selection'].get_selected_choices() + self.filter_mapper['brand'].get_selected_choices() + self.filter_mapper['category'].get_selected_choices()
        if len(selected_choices) == 1:
            return selected_choices[0].name
        elif len(selected_choices) > 1:
            return ' '.join([x.name for x in selected_choices])
        return _("Search all %s" % self.get_subject_plural())

    def get_title(self):
        selected_choices = self.filter_mapper['brand'].get_selected_choices() + self.filter_mapper['category'].get_selected_choices()
        if len(selected_choices) == 1:
            return selected_choices[0].meta_title
        elif len(selected_choices) > 1:
            try:
                return "%s %s" % (self.filter_mapper['brand'].get_selected_choices()[0].name, self.filter_mapper['category'].get_selected_choices()[0].subject_plural)
            except IndexError:
                pass
        return _("Search")

    def get_description(self):
        selected_choices = self.filter_mapper['selection'].get_selected_choices() + self.filter_mapper['brand'].get_selected_choices() + self.filter_mapper['category'].get_selected_choices()
        if len(selected_choices) == 1:
            return selected_choices[0].html
        return 'description'

    def get_subject(self):
        selected_choices = self.filter_mapper['brand'].get_selected_choices() + self.filter_mapper['category'].get_selected_choices()
        if len(selected_choices) == 1:
            try:
                return selected_choices[0].subject
            except AttributeError:
                return '%s product' % self.filter_mapper['brand'].get_selected_choices()
        return _("product")

    def get_subject_plural(self):
        selected_choices = self.filter_mapper['brand'].get_selected_choices() + self.filter_mapper['category'].get_selected_choices()
        if len(selected_choices) == 1:
            try:
                return selected_choices[0].subject_plural
            except AttributeError:
                return '%s producten' % selected_choices[0].name
        elif len(selected_choices) > 1:
            text = ''
            text += ', '.join([x.name for x in self.filter_mapper['brand'].get_selected_choices()])
            text += ' '
            text += ', '.join([x.name for x in self.filter_mapper['category'].get_selected_choices()])
            return text
        return _("products")


class Choice(object):

    def __init__(self, obj, **kwargs):
        self.obj = obj
        super(Choice, self).__init__(**kwargs)

    def __unicode__(self):
        return u"%s" % str(self.obj)

    def __str__(self):
        return str(self.obj)

    def __eq__(self, other):
        if hasattr(other, 'obj'):
            return self.obj == other.obj
        else:
            return self.obj == other


class BaseFilter(object):
    """
    Intentionally simple parent class for all list filters. Only implements
    dispatch-by-method and simple sanity checking.
    """

    name = None # Technical name
    title = None  # Human-readable title.
    template = 'filters/filter.html'
    raise_404 = True
    count = False
    meta_noindex = False
    is_visible = True

    def __init__(self, queryset, request, view, *args, **kwargs):
        """
        Constructor. Called in the view; can contain helpful extra
        keyword arguments, and other things.
        """

        self.view = view
        self.queryset = queryset
        self.request = request

        # Go through keyword arguments, and either save their values to our
        # instance, or raise an error.
        for key, value in kwargs.iteritems():
            setattr(self, key, value)

        if self.title is None:
            raise ImproperlyConfigured("The list filter '%s' does not specify a 'title'." % self.__class__.__name__)


    def __repr__(self):
        return 'Filter(%s)' % (', '.join(map(repr, (self.name, self.view))))

    def get_key(self):
        """The default key is the name of the filter"""
        return self.name

    def get_value(self, key=None):
        try:
            key = self.get_key() if not key else key
            return self.get_kwargs().get(key, '')
        except Exception, e:
            raise Exception(e)


    def cleaned_value(self):
        return self.get_value()

    #    def get_other_active_filters(self):
    #        return (x for x in self.queryset.active_filters if not x.name == self.name)

    def lookups(self, request, model_admin):
        """
        Must be overriden to return a list of tuples (value, verbose value)
        """
        raise NotImplementedError

    def get_results(self):
        """
        Returns the filtered queryset.
        """
        raise NotImplementedError

    @property
    def is_active(self):
        return True if self.get_value() else False

    @property
    def meta_noindex(self):
        if self.type == 'get':
            return True
        else:
            return False

    @property
    def rel_nofollow(self):
        return True
        return True if True in [x.meta_noindex for x in self.filterset.active_filters + [self]] else False

    def render(self):
        """
        Render the template
        """
        return render_to_string(self.template, context_instance=Context({'filter': self }))

    def reverse_path(self, kwargs):
        """
        Genereate a filter url
        """
        if self.type == 'get':
            return reverse_lazy(self.view, kwargs=resolve(self.request.path).kwargs) + '?' + urllib.urlencode(kwargs)
        elif self.type == 'slug':
            if self.request.GET:
                return reverse_lazy(self.view, kwargs=kwargs) + '?' + urllib.urlencode(self.request.GET)
            else:
                return reverse_lazy(self.view, kwargs=kwargs)

    def get_kwargs(self):
        """
        Get the kwargs for this filter according the filter type
        """
        try:
            if self.type == 'slug':
                kwargs = dict(resolve(self.request.path).kwargs.copy())
            elif self.type == 'get':
                kwargs = self.request.GET.dict()
            return self.clean_kwargs(kwargs)
        except Exception, e:
            raise Exception(e)

    def clean_kwargs(self, kwargs):
        """
        Cleanup kwargs, uniqify and sort
        """
        for key, value in kwargs.items():
            if not value:
                # if the value is empty remove the key
                del kwargs[key]
            if getattr(self, 'value_delimiter', None):
                # cleanup the value by removing doubles and sorting the values
                value_list = value.split(self.value_delimiter)
                value_list = uniqify_list(value_list)
                value_list.sort()
                kwargs[key] = self.value_delimiter.join(value_list)
        return kwargs

    def remove_filter_path(self):
        """
        Create a path to remove/deactivate the complete filter
        """
        kwargs = self.get_kwargs()
        if self.is_active:
            del kwargs[self.get_key()]
        return self.reverse_path(kwargs)

    def filter_path(self, key_values):
        """
        Create a filter path by cleaning the kwargs
        And reversing a path according those kwargs
        """
        kwargs = self.clean_kwargs(key_values)
        return self.reverse_path(kwargs)


class ChoicesFilter(BaseFilter):
    """
    Allows multiple choices selected at once
    """
    type = 'get'
    model_field = None
    template = 'filters/filter_choices.html'
    value_delimiter = ','
    count = True

    @property
    def is_visible(self):
        """
        Hide filter when there is one choice
        Hide filter when choices have no results
        """
        choices = self.get_choices()
        #if len(choices) < 2:
        #   return False
        if self.count and not [x for x in choices if x.count]:
            return False

        return True

    def cleaned_value(self):
        """
        Return the splitted (by value delimitter) if there is an value else return empty list
        """
        return self.get_value().split(self.value_delimiter) if self.get_value() else []

    @cached
    def _get_count(self):
        return [str(x) for x in self.queryset.values_list(self.model_field, flat=True)]

    def get_count(self, choice):
        return list(self._get_count()).count(str(choice))

    def choices(self):
        raise NotImplementedError

    def _get_selected_choices(self):
        return [x for x in self.choices() if x in self.cleaned_value()]

    def get_selected_choices(self):
        return [x for x in self.get_choices() if x.selected]

    @cached
    def get_choices(self):
        choices = [Choice(x) for x in self.choices()]
        for choice in choices:
            try:
                choice.selected = True if str(choice) in self._get_selected_choices() else False

                # Get all the path kwargs and change te kwarg for this choice

                # create filter url
                kwargs = self.get_kwargs()
                kwargs[self.get_key()] = self.value_delimiter.join((str(choice), kwargs.get(self.get_key(), str(choice))))
                kwargs = self.clean_kwargs(kwargs)
                choice.kwargs = kwargs
                choice.filter_path = self.reverse_path(kwargs)

                # create deselect choice url
                # remove the value in the splitted value of key
                if choice.selected:
                    rmkwargs = kwargs.copy()
                    rmvalues = rmkwargs.get(self.get_key(), '').split(self.value_delimiter)
                    test = self.value_delimiter.join([x for x in rmvalues if x != str(choice)])
                    rmkwargs[self.get_key()] = test
                    if not test:
                        del rmkwargs[self.get_key()]
                    choice.rmkwargs = rmkwargs
                    choice.filter_path = self.reverse_path(rmkwargs)

                choice.rel_nofollow = any(x in choice.filter_path for x in [self.value_delimiter, '?'])

            except Exception, e:
                raise Exception(e)
            if self.count:
                choice.count = self.get_count(str(choice))
                choice.rel_nofollow = True if not choice.count else False
        return choices

    @cached
    def get_results(self):
        try:
            return self.queryset.filter(**{'%s__in' % self.model_field: self._get_selected_choices()})
        except Exception, e:
            #TODO: log this somewhere appropiate (and uncomment the above line)
            return self.queryset.none()

class ChoiceFilter(ChoicesFilter):
    """
    Allows multiple choices selected at once
    """
    template = 'filters/filter_choice.html'
    count = True

    def cleaned_value(self):
        """
        Return the value
        """
        return self.get_value()

    @cached
    def get_choices(self):
        choices = [Choice(x) for x in self.choices()]
        for choice in choices:
            try:
                choice.selected = True if str(choice) in self._get_selected_choices() else False

                # Get all the path kwargs and change te kwarg for this choice

                # create filter url
                kwargs = self.get_kwargs()
                kwargs[self.get_key()] = str(choice)
                kwargs = self.clean_kwargs(kwargs)
                choice.kwargs = kwargs
                choice.filter_path = self.reverse_path(kwargs)

                # create deselect choice url
                # remove the value in the splitted value of key
                if choice.selected:
                    rmkwargs = kwargs.copy()
                    del rmkwargs[self.get_key()]
                    choice.rmkwargs = rmkwargs
                    choice.filter_path = self.reverse_path(rmkwargs)

                choice.rel_nofollow = any(x in choice.filter_path for x in [self.value_delimiter, '?'])

            except Exception, e:
                raise Exception(e)
            if self.count:
                choice.count = self.get_count(choice)
                choice.rel_nofollow = True if not choice.count else False
        return choices

    def get_selected_choice(self):
        try:
            return self.get_selected_choices()[0]
        except:
            return None

    @cached
    def get_results(self):
        try:
            return self.queryset.filter(**{'%s__exact' % self.model_field: self.get_selected_choice().obj})
        except Exception, e:
            #TODO: log this somewhere appropiate (and uncomment the above line)
            return self.queryset.none()


class ModelChoicesFilter(BaseFilter):
    """
    Allows multiple choices selected at once
    """
    type = 'get'
    model_field = None
    template = 'filters/filter_choices.html'
    model = None
    value_delimiter = ','
    count = True

    @property
    def is_visible(self):
        """
        Hide filter when there is one choice
        Hide filter when choices have no results
        """
        choices = self.get_choices()
        #if len(choices) < 2:
        #   return False
        if self.count and not [x for x in choices if x.count]:
            return False

        return True

    def cleaned_value(self):
        """
        Return the splitted (by value delimitter) if there is an value else return empty list
        """
        return self.get_value().split(self.value_delimiter) if self.get_value() else []

    @cached
    def _get_count(self):
        return self.queryset.values_list(self.model_field, flat=True)

    def get_count(self, choice):
        return list(self._get_count()).count(choice.pk)

    @cached
    def choices(self):
        return self.model.on_site.all()[:]

    def _get_selected_choices(self):
        return [x for x in self.choices() if x.slug in self.cleaned_value()]


    def get_selected_choices(self):
        return [x for x in self.get_choices() if x.selected]

    @cached
    def get_choices(self):
        choices = self.choices()
        for choice in choices:
            choice.selected =  True if choice in self._get_selected_choices() else False
            try:
                # Get all the path kwargs and change te kwarg for this choice

                # create filter url
                kwargs = self.get_kwargs()
                kwargs[self.get_key()] = self.value_delimiter.join((choice.slug, kwargs.get(self.get_key(), choice.slug)))
                kwargs = self.clean_kwargs(kwargs)
                choice.kwargs = kwargs
                choice.filter_path = self.reverse_path(kwargs)

                # create deselect choice url
                # remove the value in the splitted value of key
                if choice.selected:
                    rmkwargs = kwargs.copy()
                    rmvalues = rmkwargs.get(self.get_key(), '').split(self.value_delimiter)
                    test = self.value_delimiter.join([x for x in rmvalues if x != choice.slug])
                    rmkwargs[self.get_key()] = test
                    if not test:
                        del rmkwargs[self.get_key()]
                    choice.rmkwargs = rmkwargs
                    choice.filter_path = self.reverse_path(rmkwargs)

                choice.rel_nofollow = any(x in choice.filter_path for x in [self.value_delimiter, '?'])

            except Exception, e:
                raise Exception(e)
            if self.count:
                choice.count = self.get_count(choice)
                choice.rel_nofollow = True if not choice.count else choice.rel_nofollow
        return choices

    @cached
    def get_results(self):
        try:
            return self.queryset.filter(**{'%s__in' % self.model_field: self._get_selected_choices()})
        except Exception, e:
            #TODO: log this somewhere appropiate (and uncomment the above line)
            return self.queryset.none()


class ModelChoiceFilter(ModelChoicesFilter):
    template = 'filters/filter_choice.html'

    def cleaned_value(self):
        """
        Return the value
        """
        return self.get_value()

    @cached
    def get_choices(self):
        choices = self.choices()
        for choice in choices:
            choice.selected =  True if choice in self._get_selected_choices() else False
            try:
                # Get all the path kwargs and change te kwarg for this choice

                # create filter url
                kwargs = self.get_kwargs()
                kwargs[self.get_key()] = choice.slug
                kwargs = self.clean_kwargs(kwargs)
                choice.kwargs = kwargs
                choice.filter_path = self.reverse_path(kwargs)

                # create deselect choice url
                # remove the value in the splitted value of key
                if choice.selected:
                    rmkwargs = kwargs.copy()
                    del rmkwargs[self.get_key()]
                    choice.rmkwargs = rmkwargs
                    choice.filter_path = self.reverse_path(rmkwargs)

                choice.rel_nofollow = any(x in choice.filter_path for x in [self.value_delimiter, '?'])

            except Exception, e:
                raise Exception(e)
            if self.count:
                choice.count = self.get_count(choice)
                choice.rel_nofollow = True if not choice.count else choice.rel_nofollow
        return choices

    @cached
    def get_results(self):
        try:
            return self.queryset.filter(**{'%s__exact' % self.model_field: self._get_selected_choices()[0]})
        except Exception, e:
            #TODO: log this somewhere appropiate (and uncomment the above line)
            return self.queryset.none()


class RangeFilter(BaseFilter):
    type = 'get'
    model_field = None
    template = 'filters/filter_range.html'
    text_min = "Min."
    text_max = "Max."

    @property
    def is_visible(self):
        """
        Check if this filter should be visible for the current queryset
        """
        return self.value_max() - self.value_min()

    @cached
    def range_min(self):
        try:
            result = self.queryset.aggregate(Min(self.model_field)).values()[0]
            return int(math.floor(float(result))) if result else 0
        except Exception, e:
            raise Exception(e)

    @cached
    def range_max(self):
        try:
            result = self.queryset.aggregate(Max(self.model_field)).values()[0]
            return int(math.ceil(float(result))) if result else 0
        except Exception, e:
            raise Exception(e)

    def cleaned_value(self):
        value = self.get_value()
        if self.get_value():
            try:
                min, max = [int(x) for x in value.split(',')]
                return min, max
            except (ValueError, AttributeError):
                if self.get_value() and self.raise_404:
                    raise Http404
            except Exception, e:
                raise Exception(e, self.name, self.get_key(), self.get_value(), self.get_kwargs(), self.request.GET, self.request.GET)

    def value_min(self):
        if self.cleaned_value():
            return self.cleaned_value()[0]
        else:
            return self.range_min()

    def value_max(self):
        if self.cleaned_value():
            return self.cleaned_value()[1]
        else:
            return self.range_max()

    def _get_results(self, gte, lte):
        # gte is min and lte is max
        kwargs_gte = { "%s__gte" % self.model_field: gte }
        kwargs_lte = { "%s__lte" % self.model_field: lte }
        return self.queryset.filter(**kwargs_gte).filter(**kwargs_lte)

    @cached
    def get_results(self):
        gte, lte = self.value_min(), self.value_max()
        return self._get_results(gte, lte)
