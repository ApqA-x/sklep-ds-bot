package config

import (
	"fmt"
	"os"
	"strings"

	"github.com/robinlant/sklep-ds-bot/internal/botauth"
)

type Config struct {
	ServiceName          string
	MongoURI             string
	MongoDB              string
	NATSURL              string
	DiscordToken         string
	DiscordApplicationID string
	DiscordGuildID       string
	BotAdminUserIDs      []string
	EventSigningSecret   string
	TrackingMode         string
	TrackedChannelIDs    []string
}

func Load() (Config, error) {
	cfg := Config{
		ServiceName:          getenv("SERVICE_NAME", "tracker"),
		MongoURI:             getenv("MONGO_URI", "mongodb://localhost:27017"),
		MongoDB:              getenv("MONGO_DB", "voice_tracker"),
		NATSURL:              getenv("NATS_URL", "nats://localhost:4222"),
		DiscordToken:         os.Getenv("DISCORD_TOKEN"),
		DiscordApplicationID: strings.TrimSpace(os.Getenv("DISCORD_APPLICATION_ID")),
		DiscordGuildID:       strings.TrimSpace(os.Getenv("DISCORD_GUILD_ID")),
		EventSigningSecret:   strings.TrimSpace(os.Getenv("EVENT_SIGNING_SECRET")),
	}
	cfg.BotAdminUserIDs = botauth.ParseUserIDs(os.Getenv("BOT_ADMIN_USER_IDS"))

	cfg.TrackingMode = getenv("TRACKING_MODE", "")
	if raw := strings.TrimSpace(os.Getenv("TRACKED_CHANNEL_IDS")); raw != "" {
		parts := strings.Split(raw, ",")
		for _, part := range parts {
			id := strings.TrimSpace(part)
			if id != "" {
				cfg.TrackedChannelIDs = append(cfg.TrackedChannelIDs, id)
			}
		}
	}
	if cfg.TrackingMode == "" {
		if len(cfg.TrackedChannelIDs) > 0 {
			cfg.TrackingMode = "specific"
		} else {
			cfg.TrackingMode = "all"
		}
	}

	if cfg.MongoURI == "" || cfg.MongoDB == "" || cfg.NATSURL == "" {
		return Config{}, fmt.Errorf("missing required configuration")
	}

	return cfg, nil
}

func getenv(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}
