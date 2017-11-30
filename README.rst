JMESPath Playground Backend
===========================

This project contains the REST API backend for the JMESPath playground.
It allows a user to save JMESPath queries and share them.

API
===

::

  /anon/       : POST - Create a new JMESPath saved query.
  /anon/{uuid} : GET - Return info about a JMESPath query.


Payload for ``/anon/``

::

  {
   "query": "jmespath.query",
   "data": {"input": "doc"},
  }

Dev Setup
=========

1. Create virtualenv
2. ``pip install -r requirements-dev.txt``.
