package config

import "testing"

func TestLoadBotAdminUserIDs(t *testing.T) {
	t.Setenv("BOT_ADMIN_USER_IDS", "<@123>, 456\n<@!789>")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.BotAdminUserIDs) != 3 || cfg.BotAdminUserIDs[0] != "123" || cfg.BotAdminUserIDs[1] != "456" || cfg.BotAdminUserIDs[2] != "789" {
		t.Fatalf("bot admin ids = %#v", cfg.BotAdminUserIDs)
	}
}
