import django_filters
from .models import Account, Tag
from django.db.models import Q
from django import forms

class AccountFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method='filter_search', label="Qidirish")

    BOOL_CHOICES = (
        (True, "Aktiv"),
        (False, "Aktivmas"),
    )
    is_active = django_filters.ChoiceFilter(choices=BOOL_CHOICES, label="Holati", empty_label="Barchasi")

    SPAM_CHOICES = (
        (True, "Spam"),
        (False, "Spammas"),
    )
    is_spam = django_filters.ChoiceFilter(choices=SPAM_CHOICES, label="Spam holati", empty_label="Barchasi")

    country_code = django_filters.ChoiceFilter(method='filter_country', label="Davlat", empty_label="Barchasi")

    tags = django_filters.ModelMultipleChoiceFilter(
        queryset=Tag.objects.none(),  # restricted per-request in __init__
        label="Teglar",
    )

    o = django_filters.OrderingFilter(
        choices=(
            ('-groups_count', "Guruhlar ko'p"),
            ('groups_count', "Guruhlar kam"),
            ('-channels_count', "Kanallar ko'p"),
            ('channels_count', "Kanallar kam"),
            ('-created_at', "Eng so'nggi yaratilgan"),
            ('created_at', "Eng eski yaratilgan"),
        ),
        label="Saralash (Tartib)",
        empty_label="Odatiy (Vaqt bo'yicha)"
    )

    class Meta:
        model = Account
        fields = ['search', 'is_active', 'is_spam', 'country_code', 'tags']

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        # Tags dropdown restricted to the requesting user (security).
        if user is not None:
            self.filters['tags'].queryset = Tag.objects.filter(owner=user).order_by('name')

        # Country-code choices derived from this user's own accounts only.
        base_qs = Account.objects.filter(owner=user) if user is not None else Account.objects.none()
        codes = set()
        for bot in base_qs:
            if bot.country_code:
                codes.add(bot.country_code)

        choices = []
        for code in codes:
            if code and len(code) >= 2:
                choices.append((code, code.upper()))
        self.filters['country_code'].extra['choices'] = choices

    @property
    def form(self):
        f = super().form
        for field_name in f.fields:
            is_text = field_name == 'search'
            f.fields[field_name].widget.attrs.update({
                'class': 'form-control rounded-pill bg-light border-0 fw-medium' if is_text else 'form-select rounded-pill bg-light border-0 fw-medium'
            })
            if field_name == 'country_code':
                f.fields[field_name].widget.attrs['class'] += ' country-select'
        if 'search' in f.fields:
            f.fields['search'].widget.attrs['placeholder'] = 'Ism, username yoki raqam...'
        return f

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(phone_number__icontains=value) |
            Q(first_name__icontains=value) |
            Q(last_name__icontains=value) |
            Q(username__icontains=value)
        )
        
    def filter_country(self, queryset, name, value):
        if not value: return queryset
        ids = [bot.id for bot in queryset if bot.country_code == value]
        return queryset.filter(id__in=ids)
