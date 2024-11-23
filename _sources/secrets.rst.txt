Managing Secrets
================

Unfurl allows you to store secrets separately from the rest for your configuration, either in separate configuration file or in a secrets manager such as HashiCorp Vault. In the project configuration file you specify how those secrets should be managed. You can apply the following strategies:

* Store the secrets in a local configuration file and distribute that file out-of-band if necessary.
* Encrypt the secrets file using `Ansible Vault <https://docs.ansible.com/ansible/latest/user_guide/vault.html>`_ so they can be safely checked into its git repository.
* Store them in a secrets manager such as HashiCorp Vault or Amazon Secrets Manager or your OS's keyring. You can use any secrets manager that has an `Ansible Lookup Plugin <https://docs.ansible.com/ansible/latest/plugins/lookup.html>`_ available for it.
* If your git repository is private and the secrets not highly sensitive you can just commit it into the repository in plain text.

You can apply any of these techniques to different secrets and projects can inherit the secrets configuration from :ref:`unfurl_home<Unfurl Home>`.

.. code-block:: YAML

  secrets:

      # Include secrets from a file that will be automatically encrypted when committed to the repository:
      +?include: secrets/secrets.yaml

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


Sensitive Values
----------------

You can mark configuration data as sensitive in their TOSCA definitions or by using the :std:ref:`sensitive` expression function. When those values are saved in YAML, if you have Ansible Vault secret associated with your ensemble, their encrypted values will be embedded, if not, they will be saved as "<<<Redacted>>>". When loading a YAML configuration file, any Vault data will be decrypted and any attribute with a value of "<<<Redacted>>>" will be omitted. 

Vault secrets
-------------

When creating a new ensemble via a ``unfurl init`` or ``unfurl clone``, if a new git repository is created (either because its a new project or because the ensemble uses a separate repository) will add a ``vault_secrets`` secret with a generated password to ``local/unfurl.yaml`` and ``secrets/secrets.yaml`` file added to the repository -- see `Project files`.

You can force this in any new project by passing the``VAULT_PASSWORD`` skeleton variable to ``unfurl init`` or ``unfurl clone``. See `setting <vault_password_var>` for an example.

.. important::

  Store this master Vault password found in ``local/unfurl.yaml`` in a safe place!

Creating secrets
----------------

When ``unfurl commit`` commits changes to a project, any files in directories named ``secrets`` will automatically be encrypted with the project's vault password and committed into a parallel directory named ``.secrets``. When Unfurl starts it will automatically decrypt those files and restore them to their ``secrets`` directory.

To create secrets manually (for example, to use inline as shown in the example above), you can use the ``unfurl-vault-client`` script with the ``ansible-vault`` command. The ``unfurl-vault-client`` script outputs the vault password for the current project so you can encrypt secrets using the ``ansible-vault`` utility like this:

.. code-block::

  ansible-vault encrypt_string --vault-id default@unfurl-vault-client "secret1" "secret2"
