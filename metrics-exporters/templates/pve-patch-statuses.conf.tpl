{% for patch, status_path, successful_states in PVE_PATCH_STATUSES -%}
{{ patch }}|{{ status_path }}|{{ successful_states }}
{% endfor %}
