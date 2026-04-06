package shuffle

import (
	"testing"

	"github.com/bwmarrin/discordgo"
)

func TestCanUseShuffleCommand(t *testing.T) {
	manage := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{Permissions: discordgo.PermissionVoiceMoveMembers}}}
	admin := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{Permissions: discordgo.PermissionAdministrator}}}
	plain := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{Permissions: discordgo.PermissionManageGuild}}}
	allowlisted := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{User: &discordgo.User{ID: "u1"}}}}

	if !canUseShuffleCommand(manage, nil) {
		t.Fatal("expected move members to pass")
	}
	if !canUseShuffleCommand(admin, nil) {
		t.Fatal("expected admin to pass")
	}
	if !canUseShuffleCommand(allowlisted, []string{"u1"}) {
		t.Fatal("expected allowlisted user to pass")
	}
	if canUseShuffleCommand(plain, nil) {
		t.Fatal("expected manage guild alone to fail")
	}
}

func TestShuffleApplicationCommandHasNoDefaultPermissions(t *testing.T) {
	command := ShuffleApplicationCommand()
	if command.DefaultMemberPermissions != nil {
		t.Fatal("expected shuffle command to be visible by default")
	}
}
