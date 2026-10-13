{% for patch, status_path, successful_states, markers in PVE_PATCH_STATUSES -%}
{{ patch }}|{{ status_path }}|{{ successful_states }}|{{ markers }}
{% endfor %}
