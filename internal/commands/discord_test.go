package commands

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/bwmarrin/discordgo"
	"github.com/robinlant/sklep-ds-bot/internal/domain"
)

func testInteraction(guildID string, perms int64, channels map[string]*discordgo.Channel) *discordgo.InteractionCreate {
	return &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{
		Type:    discordgo.InteractionApplicationCommand,
		GuildID: guildID,
		Member:  &discordgo.Member{Permissions: perms},
		Data:    discordgo.ApplicationCommandInteractionData{Resolved: &discordgo.ApplicationCommandInteractionDataResolved{Channels: channels}},
	}}
}

func TestParseVoiceRoute(t *testing.T) {
	group, command, opts := parseVoiceRoute([]*discordgo.ApplicationCommandInteractionDataOption{{
		Type: discordgo.ApplicationCommandOptionSubCommandGroup,
		Name: "config",
		Options: []*discordgo.ApplicationCommandInteractionDataOption{{
			Type:    discordgo.ApplicationCommandOptionSubCommand,
			Name:    "channels",
			Options: []*discordgo.ApplicationCommandInteractionDataOption{{Name: "action", Value: "list"}},
		}},
	}})
	if group != "config" || command != "channels" || len(opts) != 1 {
		t.Fatalf("unexpected route: %s %s %#v", group, command, opts)
	}
}

func TestCanUseVoiceCommand(t *testing.T) {
	manage := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{Permissions: discordgo.PermissionManageGuild}}}
	admin := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{Permissions: discordgo.PermissionAdministrator}}}
	allowlisted := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{User: &discordgo.User{ID: "u1"}}}}
	if !canUseVoiceCommand(manage, nil, "config", "channels") {
		t.Fatal("expected manage guild to pass config")
	}
	if !canUseVoiceCommand(admin, nil, "inspect", "sessions") {
		t.Fatal("expected admin to pass session inspection")
	}
	if !canUseVoiceCommand(allowlisted, []string{"u1"}, "inspect", "sessions") {
		t.Fatal("expected allowlisted user to pass all commands")
	}
	if canUseVoiceCommand(manage, nil, "inspect", "sessions") {
		t.Fatal("expected manage guild to fail session inspection")
	}
}

func TestHandleConfigCommandChannelsAdd(t *testing.T) {
	repo := newFakeRepo()
	svc := New(repo)
	interaction := testInteraction("g1", discordgo.PermissionManageGuild, map[string]*discordgo.Channel{"c1": {ID: "c1", GuildID: "g1", Type: discordgo.ChannelTypeGuildVoice}})
	content, err := svc.handleConfigCommand(context.Background(), interaction, "channels", []*discordgo.ApplicationCommandInteractionDataOption{
		{Name: "action", Value: "add"},
		{Name: "channel", Value: "c1"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "tracking mode: all") || !strings.Contains(content, "stored channels: <#c1>") {
		t.Fatalf("unexpected content: %s", content)
	}
}

func TestHandleConfigCommandChannelsRemoveListClear(t *testing.T) {
	repo := newFakeRepo()
	repo.settings["g1"] = domain.NewGuildSettings("g1", domain.GuildTrackingModeSpecific, []string{"c1", "c2"}, "")
	svc := New(repo)
	interaction := testInteraction("g1", discordgo.PermissionManageGuild, map[string]*discordgo.Channel{"c1": {ID: "c1", GuildID: "g1", Type: discordgo.ChannelTypeGuildVoice}})

	content, err := svc.handleConfigCommand(context.Background(), interaction, "channels", []*discordgo.ApplicationCommandInteractionDataOption{{Name: "action", Value: "remove"}, {Name: "channel", Value: "c1"}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "<#c2>") {
		t.Fatalf("unexpected remove content: %s", content)
	}

	content, err = svc.handleConfigCommand(context.Background(), interaction, "channels", []*discordgo.ApplicationCommandInteractionDataOption{{Name: "action", Value: "list"}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "<#c2>") {
		t.Fatalf("unexpected list content: %s", content)
	}

	content, err = svc.handleConfigCommand(context.Background(), interaction, "channels", []*discordgo.ApplicationCommandInteractionDataOption{{Name: "action", Value: "clear"}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "no voice channels") {
		t.Fatalf("unexpected clear content: %s", content)
	}
}

func TestHandleInspectCommandSessions(t *testing.T) {
	repo := newFakeRepo()
	repo.sessions["s1"] = domain.Session{ID: "s1", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusActive, StartedAt: time.Date(2026, 4, 5, 18, 0, 0, 0, time.UTC)}
	svc := New(repo)
	interaction := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{GuildID: "g1", Member: &discordgo.Member{Permissions: discordgo.PermissionAdministrator}}}
	content, err := svc.handleInspectCommand(context.Background(), interaction, "sessions", nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "<#c1>") {
		t.Fatalf("unexpected content: %s", content)
	}
}

func TestHandleInspectCommandSessionsEmpty(t *testing.T) {
	repo := newFakeRepo()
	svc := New(repo)
	interaction := testInteraction("g1", discordgo.PermissionAdministrator, nil)
	content, err := svc.handleInspectCommand(context.Background(), interaction, "sessions", nil)
	if err != nil {
		t.Fatal(err)
	}
	if content != "No active sessions." {
		t.Fatalf("unexpected content: %s", content)
	}
}

func TestHandleInspectCommandSession(t *testing.T) {
	repo := newFakeRepo()
	repo.settings["g1"] = domain.NewGuildSettings("g1", domain.GuildTrackingModeNone, nil, "")
	started := time.Date(2026, 4, 5, 18, 0, 0, 0, time.UTC)
	repo.sessions["s1"] = domain.Session{ID: "s1", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusActive, StartedAt: started}
	repo.participants["s1"] = []domain.ParticipantInterval{{ID: "p1", SessionID: "s1", GuildID: "g1", ChannelID: "c1", UserID: "u1", UserName: "alice", JoinedAt: started.Add(5 * time.Minute), Active: true}}
	svc := New(repo)
	interaction := testInteraction("g1", discordgo.PermissionAdministrator, map[string]*discordgo.Channel{"c1": {ID: "c1", GuildID: "g1", Type: discordgo.ChannelTypeGuildVoice}})
	content, err := svc.handleInspectCommand(context.Background(), interaction, "session", []*discordgo.ApplicationCommandInteractionDataOption{{Name: "channel", Value: "c1"}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "alice") || !strings.Contains(content, "<#c1>") {
		t.Fatalf("unexpected content: %s", content)
	}
}

func TestHandleInspectCommandSessionMissing(t *testing.T) {
	repo := newFakeRepo()
	repo.settings["g1"] = domain.NewGuildSettings("g1", domain.GuildTrackingModeNone, nil, "")
	svc := New(repo)
	interaction := testInteraction("g1", discordgo.PermissionAdministrator, map[string]*discordgo.Channel{"c1": {ID: "c1", GuildID: "g1", Type: discordgo.ChannelTypeGuildVoice}})
	content, err := svc.handleInspectCommand(context.Background(), interaction, "session", []*discordgo.ApplicationCommandInteractionDataOption{{Name: "channel", Value: "c1"}})
	if err != nil {
		t.Fatal(err)
	}
	if content != "No active session in that channel." {
		t.Fatalf("unexpected content: %s", content)
	}
}
