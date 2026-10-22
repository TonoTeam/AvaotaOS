# SPDX-License-Identifier: GPL-3.0
# Native logind is needed for subject.active/local in the Wi-Fi authorization rule.
# Buildroot's upstream polkit package defaults to ConsoleKit. Keep systemd's own
# polkit integration disabled, otherwise these dependencies would be circular.
ifeq ($(BR2_INIT_SYSTEMD)$(BR2_PACKAGE_POLKIT),yy)
POLKIT_CONF_OPTS := $(filter-out -Dsession_tracking=ConsoleKit,$(POLKIT_CONF_OPTS)) -Dsession_tracking=logind
POLKIT_DEPENDENCIES += systemd
# Built-in package rules were evaluated before external.mk. Add the real stamp
# prerequisite too, so an ordinary parallel world build stages libsystemd first.
$(POLKIT_TARGET_CONFIGURE): | systemd
polkit-depends: systemd
endif

include $(sort $(wildcard $(BR2_EXTERNAL_TONO_A527_PATH)/package/*/*.mk))
