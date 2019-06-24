#! /usr/bin/env python

from __future__ import absolute_import, division, print_function, unicode_literals

import sys, os
import logging

sys.path.append("./")

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)-5.5s %(name)-20.20s %(levelname)-7.7s %(message)s",
    datefmt="%H:%M",
    level=logging.DEBUG,
)

from simulate import simulate_train_marginalref

logging.info("Hi!")

simulate_train_marginalref(n=10, n_thetas_marginal=None)

logging.info("All done! Have a nice day!")
