Managing Secrets
================

Unfurl allows you to store secrets separately from the rest for your configuration, either in separate configuration file or in a secrets manager such as HashiCorp Vault. In the project configuration file you specify how those secrets should be managed. You can apply the following strategies:

* Store the secrets in a local configuration file and distribute that file out-of-band if necessary.
* Encrypt the secrets file using `Ansible Vault <https://docs.ansible.com/ansible/latest/user_guide/vault.html>`_ so they can be safely checked into the ensemble repository.
* Store them in a secrets manager such as HashiCorp Vault or Amazon Secrets Manager or your OS's keyring. You can use any secrets manager that has an `Ansible Lookup Plugin <https://docs.ansible.com/ansible/latest/plugins/lookup.html>`_ available for it.
* If your ensemble repository is private and the secrets not highly sensitive you can just commit it into the repository in plain text.

You can apply any of these techniques to different secrets and projects can inherit the secrets configuration from :ref:`unfurl_home<configure>`.

.. code-block:: YAML

  secrets:
    attributes:
      # include secrets from a file that will not be committed to the repository:
      +?include: local/secrets.yaml
      # plaintext:
      not_so_secret: admin

      # encrypted inlined:
      the_dev_secret: !vault |
        $ANSIBLE_VAULT;1.2;AES256;dev
        30613233633461343837653833666333643061636561303338373661313838333565653635353162
        3263363434623733343538653462613064333634333464660a663633623939393439316636633863
        61636237636537333938306331383339353265363239643939666639386530626330633337633833
        6664656334373166630a363736393262666465663432613932613036303963343263623137386239
        6330
      # if secret isn't defined above look it up in a HashiCorp Vault instance
      # (assumes VAULT_TOKEN etc. environment variables are set)
      default: "{{ lookup('hashi_vault', 'secret='+key) }}" # "key" will be set to the secret name


The "unfurl-vault-client" script outputs the vault password for the current project so you can encrypt secrets using the ``ansible-vault`` utility like this:

.. code-block::

  ansible-vault encrypt_string --vault-id default@unfurl-vault-client "secret1" "secret2"

Sensitive Values
----------------
You can mark configuration data as sensitive. If you have Ansible Vault ids associated with your ensemble that will be saved encrypted, if not, they will be saved as "<<<Redacted>>>". When loading a YAML configuration file, any Vault data will be decrypted and any attribute with a value of "<<<Redacted>>>" will be omitted. By default, "unfurl init" will generate a random Ansible Vault key to your local secrets (found in "local/unfurl.yaml") and so any data marked sensitive will be encrypted.

Creating secrets
----------------

.. code-block::

  ansible-vault encrypt_string --vault-id unfurl-vault-client
