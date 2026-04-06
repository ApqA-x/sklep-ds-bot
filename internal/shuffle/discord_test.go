package shuffle

import (
	"testing"

	"github.com/bwmarrin/discordgo"
)

func TestCanUseShuffleCommand(t *testing.T) {
	manage := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{Permissions: discordgo.PermissionVoiceMoveMembers}}}
	admin := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{Permissions: discordgo.PermissionAdministrator}}}
	plain := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{Permissions: discordgo.PermissionManageGuild}}}

	if !canUseShuffleCommand(manage) {
		t.Fatal("expected move members to pass")
	}
	if !canUseShuffleCommand(admin) {
		t.Fatal("expected admin to pass")
	}
	if canUseShuffleCommand(plain) {
		t.Fatal("expected manage guild alone to fail")
	}
}
